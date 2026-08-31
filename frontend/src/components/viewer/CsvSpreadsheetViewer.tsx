import { useState, useMemo, useCallback } from "react"
import Papa from "papaparse"
import { List } from "react-window"
import { Search, ArrowUpDown, ArrowUp, ArrowDown, Table, Code, Filter, AlertTriangle, FileUp } from "lucide-react"
import { Input } from "../ui/input"
import { Button } from "../ui/button"

interface CsvSpreadsheetViewerProps {
  csvText: string
  filename?: string
  isLargeFile?: boolean
}

const MAX_PREVIEW_ROWS = 1000

function isBinaryData(text: string): boolean {
  if (!text) return false
  if (text.startsWith("%PDF-") || text.includes("/Linearized") || text.includes("endobj")) {
    return true
  }
  const sample = text.slice(0, 800)
  let nullsOrControl = 0
  for (let i = 0; i < sample.length; i++) {
    const code = sample.charCodeAt(i)
    if (code === 0) return true
    if (code < 9 || (code > 13 && code < 32)) {
      nullsOrControl++
    }
  }
  return nullsOrControl > 15
}

function parseCSVWithPapa(text: string, previewRows = MAX_PREVIEW_ROWS): {
  headers: string[]
  rows: string[][]
  isBinary: boolean
  totalEstimatedRows: number
} {
  if (!text || !text.trim()) {
    return { headers: [], rows: [], isBinary: false, totalEstimatedRows: 0 }
  }

  if (isBinaryData(text)) {
    return { headers: [], rows: [], isBinary: true, totalEstimatedRows: 0 }
  }

  const parsed = Papa.parse<string[]>(text, {
    skipEmptyLines: "greedy",
    preview: previewRows + 1,
    delimitersToGuess: [",", "\t", "|", ";"]
  })

  if (!parsed.data || parsed.data.length === 0) {
    return { headers: [], rows: [], isBinary: false, totalEstimatedRows: 0 }
  }

  const firstRow = (parsed.data[0] || []).map((h, i) =>
    h ? String(h).trim().replace(/\r?\n/g, " ") : `Column ${i + 1}`
  )
  const headers = firstRow.length > 0 ? firstRow : ["Column 1"]
  const numCols = headers.length

  const rows: string[][] = []
  for (let i = 1; i < parsed.data.length; i++) {
    const rawRow = parsed.data[i]
    if (!rawRow || rawRow.length === 0) continue
    const row = rawRow.map((cell) =>
      cell !== undefined && cell !== null ? String(cell).trim() : ""
    )
    while (row.length < numCols) row.push("")
    rows.push(row.slice(0, numCols))
  }

  const totalEstimatedRows = text.split("\n").length - 1

  return {
    headers,
    rows,
    isBinary: false,
    totalEstimatedRows: Math.max(rows.length, totalEstimatedRows)
  }
}

interface RowData {
  rows: string[][]
  headers: string[]
  filteredIndices: number[]
  colWidthPercent: number
}

const RowComponent = ({
  index,
  style,
  rows,
  headers,
  filteredIndices,
  colWidthPercent
}: {
  index: number
  style: React.CSSProperties
  rows: string[][]
  headers: string[]
  filteredIndices: number[]
  colWidthPercent: number
}) => {
  const actualRowIdx = filteredIndices[index]
  const row = rows[actualRowIdx] || []
  const isEven = index % 2 === 0

  return (
    <div
      style={style}
      className={`flex items-center border-b border-border/40 font-mono text-xs hover:bg-accent/50 transition-colors ${
        isEven ? "bg-card/40" : "bg-card/90"
      }`}
    >
      {/* Row Index Number */}
      <div className="w-14 shrink-0 px-3 py-2 text-center text-muted-foreground border-r border-border/40 select-none text-[11px] font-semibold">
        {actualRowIdx + 1}
      </div>

      {/* Cells */}
      <div className="flex-1 flex min-w-0">
        {headers.map((_, colIdx) => (
          <div
            key={colIdx}
            style={{ width: `${colWidthPercent}%`, minWidth: "140px" }}
            className="shrink-0 px-3 py-2 text-foreground truncate border-r border-border/20 last:border-r-0"
            title={row[colIdx] || ""}
          >
            {row[colIdx] || <span className="text-muted-foreground/40 italic">-</span>}
          </div>
        ))}
      </div>
    </div>
  )
}

export function CsvSpreadsheetViewer({ csvText, filename = "data.csv" }: CsvSpreadsheetViewerProps) {
  const [currentText, setCurrentText] = useState<string>(csvText)
  const [viewMode, setViewMode] = useState<"grid" | "raw">("grid")
  const [searchTerm, setSearchTerm] = useState("")
  const [sortCol, setSortCol] = useState<number | null>(null)
  const [sortAsc, setSortAsc] = useState(true)

  useMemo(() => {
    setCurrentText(csvText)
  }, [csvText])

  const { headers, rows, isBinary, totalEstimatedRows } = useMemo(
    () => parseCSVWithPapa(currentText),
    [currentText]
  )

  const colWidthPercent = useMemo(() => {
    return headers.length > 0 ? 100 / headers.length : 100
  }, [headers.length])

  const tableMinWidth = useMemo(() => {
    return Math.max(700, 56 + headers.length * 150)
  }, [headers.length])

  const filteredAndSortedIndices = useMemo(() => {
    let indices = rows.map((_, idx) => idx)

    if (searchTerm.trim()) {
      const q = searchTerm.toLowerCase()
      indices = indices.filter((rowIdx) => {
        const row = rows[rowIdx]
        return row.some((cell) => cell.toLowerCase().includes(q))
      })
    }

    if (sortCol !== null && sortCol < headers.length) {
      indices.sort((a, b) => {
        const valA = rows[a][sortCol] || ""
        const valB = rows[b][sortCol] || ""

        const numA = Number(valA.replace(/[$,]/g, ""))
        const numB = Number(valB.replace(/[$,]/g, ""))

        if (!isNaN(numA) && !isNaN(numB) && valA.trim() !== "" && valB.trim() !== "") {
          return sortAsc ? numA - numB : numB - numA
        }

        return sortAsc ? valA.localeCompare(valB) : valB.localeCompare(valA)
      })
    }

    return indices
  }, [rows, headers.length, searchTerm, sortCol, sortAsc])

  const handleSort = (colIdx: number) => {
    if (sortCol === colIdx) {
      if (sortAsc) {
        setSortAsc(false)
      } else {
        setSortCol(null)
        setSortAsc(true)
      }
    } else {
      setSortCol(colIdx)
      setSortAsc(true)
    }
  }

  const rowProps: RowData = useMemo(
    () => ({
      rows,
      headers,
      filteredIndices: filteredAndSortedIndices,
      colWidthPercent
    }),
    [rows, headers, filteredAndSortedIndices, colWidthPercent]
  )

  const renderRow = useCallback(
    (props: any) => {
      return (
        <RowComponent
          index={props.index}
          style={props.style}
          rows={rowProps.rows}
          headers={rowProps.headers}
          filteredIndices={rowProps.filteredIndices}
          colWidthPercent={rowProps.colWidthPercent}
        />
      )
    },
    [rowProps]
  )

  if (isBinary) {
    return (
      <div className="h-96 flex flex-col items-center justify-center p-8 text-center bg-card border border-border/80 rounded-2xl space-y-3">
        <AlertTriangle className="h-10 w-10 text-amber-500 opacity-80" />
        <h3 className="font-semibold text-foreground text-sm">Binary / Encoded Stream Detected</h3>
        <p className="text-xs text-muted-foreground max-w-md leading-relaxed">
          The server returned raw binary data (PDF or encrypted stream) for {filename}. Binary files cannot be parsed as plain CSV text.
        </p>
      </div>
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
              placeholder={`Search across ${headers.length} columns...`}
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="pl-8 h-8 text-xs bg-background rounded-lg border-border/60"
            />
          </div>
          {searchTerm && (
            <span className="text-[11px] text-muted-foreground shrink-0">
              {filteredAndSortedIndices.length.toLocaleString()} matches
            </span>
          )}
        </div>

        <div className="flex items-center flex-wrap gap-2">
          {/* Metadata badges */}
          <span className="px-2.5 py-1 rounded-md bg-[#ede9de] dark:bg-[#282a2c] text-foreground/80 font-medium text-[11px]">
            {headers.length} Columns · Showing {rows.length.toLocaleString()} of ~{totalEstimatedRows.toLocaleString()} Rows
          </span>

          {/* Local CSV file upload button */}
          <div className="hidden sm:block">
            <label className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border border-border/60 bg-background text-muted-foreground hover:text-foreground text-xs font-medium cursor-pointer shadow-2xs transition-colors">
              <FileUp className="h-3 w-3" />
              <span>Open Local CSV</span>
              <input
                type="file"
                accept=".csv,.tsv,.txt"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  if (file) {
                    const reader = new FileReader()
                    reader.onload = (evt) => {
                      const text = evt.target?.result as string
                      if (text) setCurrentText(text)
                    }
                    reader.readAsText(file)
                  }
                }}
              />
            </label>
          </div>

          {/* Grid / Raw Toggle */}
          <div className="flex items-center bg-background rounded-lg p-0.5 border border-border/60">
            <button
              type="button"
              onClick={() => setViewMode("grid")}
              className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium transition-colors cursor-pointer ${
                viewMode === "grid"
                  ? "bg-primary text-primary-foreground shadow-xs"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              <Table className="h-3 w-3" />
              <span>Grid</span>
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
              <span>Raw CSV</span>
            </button>
          </div>
        </div>
      </div>

      {/* Main Content Area with unified horizontal scroll container */}
      {viewMode === "grid" ? (
        <div className="flex-1 flex flex-col min-h-0 overflow-x-auto">
          <div style={{ minWidth: `${tableMinWidth}px`, width: "100%" }} className="flex flex-col flex-1">
            {/* Sticky Table Header */}
            <div className="flex items-center bg-muted/40 border-b border-border/70 text-[11px] font-bold text-muted-foreground uppercase tracking-wider select-none shrink-0 sticky top-0 z-10">
              <div className="w-14 shrink-0 px-3 py-3 text-center border-r border-border/50">#</div>
              <div className="flex-1 flex min-w-0">
                {headers.map((header, colIdx) => (
                  <button
                    key={colIdx}
                    type="button"
                    onClick={() => handleSort(colIdx)}
                    style={{ width: `${colWidthPercent}%`, minWidth: "140px" }}
                    className="shrink-0 px-3 py-3 text-left border-r border-border/30 last:border-r-0 hover:bg-accent/40 flex items-center justify-between gap-1 transition-colors group cursor-pointer"
                    title={`Sort by ${header}`}
                  >
                    <span className="truncate">{header}</span>
                    <span className="shrink-0 text-muted-foreground group-hover:text-foreground">
                      {sortCol === colIdx ? (
                        sortAsc ? (
                          <ArrowUp className="h-3 w-3 text-primary" />
                        ) : (
                          <ArrowDown className="h-3 w-3 text-primary" />
                        )
                      ) : (
                        <ArrowUpDown className="h-3 w-3 opacity-30 group-hover:opacity-100" />
                      )}
                    </span>
                  </button>
                ))}
              </div>
            </div>

            {/* Virtualized Row Window */}
            <div className="flex-1 min-h-[480px] w-full">
              {filteredAndSortedIndices.length === 0 ? (
                <div className="h-64 flex flex-col items-center justify-center text-muted-foreground text-xs">
                  <Filter className="h-8 w-8 mb-2 opacity-30" />
                  <p>No matching rows found</p>
                  {searchTerm && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setSearchTerm("")}
                      className="mt-2 text-primary hover:text-primary/80 text-xs"
                    >
                      Clear Search Filter
                    </Button>
                  )}
                </div>
              ) : (
                <List
                  rowCount={filteredAndSortedIndices.length}
                  rowHeight={36}
                  rowProps={rowProps}
                  rowComponent={renderRow}
                  style={{ height: "100%", width: "100%", minHeight: 480 }}
                />
              )}
            </div>
          </div>
        </div>
      ) : (
        <div className="flex-1 p-4 overflow-auto font-mono text-xs bg-[#1e1e1e] text-[#d4d4d4] rounded-b-2xl">
          <pre className="whitespace-pre overflow-x-auto leading-relaxed">
            {currentText.slice(0, 200_000)}
            {currentText.length > 200_000 && "\n\n... [Raw view truncated for performance. Download full file to view all]"}
          </pre>
        </div>
      )}
    </div>
  )
}
