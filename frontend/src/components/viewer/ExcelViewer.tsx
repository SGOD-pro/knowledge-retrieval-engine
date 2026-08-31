import { useState, useMemo, useEffect, useCallback } from "react"
import * as XLSX from "xlsx"
import { List } from "react-window"
import { Search, Sheet, ArrowUpDown, ArrowUp, ArrowDown, Filter, FileSpreadsheet } from "lucide-react"
import { Input } from "../ui/input"
import { Button } from "../ui/button"

interface ExcelViewerProps {
  arrayBuffer: ArrayBuffer
  filename?: string
  isLargeFile?: boolean
}

interface ParsedSheet {
  name: string
  headers: string[]
  rows: (string | number)[][]
}

interface RowData {
  rows: (string | number)[][]
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
  rows: (string | number)[][]
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
            title={String(row[colIdx] ?? "")}
          >
            {row[colIdx] !== undefined && row[colIdx] !== null && String(row[colIdx]) !== "" ? (
              String(row[colIdx])
            ) : (
              <span className="text-muted-foreground/40 italic">-</span>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

export function ExcelViewer({ arrayBuffer, isLargeFile }: ExcelViewerProps) {
  const [sheets, setSheets] = useState<ParsedSheet[]>([])
  const [activeSheetIndex, setActiveSheetIndex] = useState(0)
  const [searchTerm, setSearchTerm] = useState("")
  const [sortCol, setSortCol] = useState<number | null>(null)
  const [sortAsc, setSortAsc] = useState(true)
  const [parseError, setParseError] = useState<string | null>(null)

  useEffect(() => {
    try {
      const readOptions: XLSX.ParsingOptions = {
        type: "array",
        dense: true,
        sheetRows: isLargeFile ? 400 : 800
      }

      const workbook = XLSX.read(arrayBuffer, readOptions)
      const parsed: ParsedSheet[] = []

      workbook.SheetNames.forEach((sheetName) => {
        const worksheet = workbook.Sheets[sheetName]
        const rawJson = XLSX.utils.sheet_to_json<(string | number)[]>(worksheet, {
          header: 1,
          defval: ""
        })

        if (rawJson && rawJson.length > 0) {
          const firstRow = rawJson[0] as (string | number)[]
          const maxCols = Math.max(
            firstRow.length,
            ...rawJson.slice(1, 50).map((r) => ((r as (string | number)[]).length))
          )

          const headers = Array.from({ length: maxCols }, (_, idx) => {
            const val = firstRow[idx]
            return val !== undefined && val !== null && String(val).trim() !== ""
              ? String(val).trim()
              : `Column ${idx + 1}`
          })

          const dataRows = rawJson.slice(1) as (string | number)[][]
          parsed.push({
            name: sheetName,
            headers,
            rows: dataRows
          })
        } else {
          parsed.push({
            name: sheetName,
            headers: ["Column 1"],
            rows: []
          })
        }
      })

      setSheets(parsed)
      setActiveSheetIndex(0)
      setParseError(null)
    } catch (err: any) {
      console.error("Excel parse error:", err)
      setParseError(err?.message || "Failed to parse Excel spreadsheet")
    }
  }, [arrayBuffer, isLargeFile])

  const currentSheet = sheets[activeSheetIndex] || { name: "", headers: [], rows: [] }

  const colWidthPercent = useMemo(() => {
    return currentSheet.headers.length > 0 ? 100 / currentSheet.headers.length : 100
  }, [currentSheet.headers.length])

  const tableMinWidth = useMemo(() => {
    return Math.max(700, 56 + currentSheet.headers.length * 150)
  }, [currentSheet.headers.length])

  const filteredAndSortedIndices = useMemo(() => {
    const rows = currentSheet.rows
    let indices = rows.map((_, idx) => idx)

    if (searchTerm.trim()) {
      const q = searchTerm.toLowerCase()
      indices = indices.filter((rowIdx) => {
        const row = rows[rowIdx]
        return row.some((cell) => String(cell).toLowerCase().includes(q))
      })
    }

    if (sortCol !== null && sortCol < currentSheet.headers.length) {
      indices.sort((a, b) => {
        const valA = String(rows[a][sortCol] ?? "")
        const valB = String(rows[b][sortCol] ?? "")

        const numA = Number(valA.replace(/[$,]/g, ""))
        const numB = Number(valB.replace(/[$,]/g, ""))

        if (!isNaN(numA) && !isNaN(numB) && valA.trim() !== "" && valB.trim() !== "") {
          return sortAsc ? numA - numB : numB - numA
        }

        return sortAsc ? valA.localeCompare(valB) : valB.localeCompare(valA)
      })
    }

    return indices
  }, [currentSheet.rows, currentSheet.headers.length, searchTerm, sortCol, sortAsc])

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
      rows: currentSheet.rows,
      headers: currentSheet.headers,
      filteredIndices: filteredAndSortedIndices,
      colWidthPercent
    }),
    [currentSheet.rows, currentSheet.headers, filteredAndSortedIndices, colWidthPercent]
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

  if (parseError) {
    return (
      <div className="h-96 flex flex-col items-center justify-center p-6 text-center bg-card border border-border/80 rounded-2xl">
        <FileSpreadsheet className="h-10 w-10 text-muted-foreground mb-3 opacity-40" />
        <h3 className="font-semibold text-foreground text-sm">Unable to parse Excel file</h3>
        <p className="text-xs text-muted-foreground mt-1 max-w-md">{parseError}</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full bg-card border border-border/80 rounded-2xl overflow-hidden shadow-xs">
      {/* Workbook Sheet Tabs */}
      {sheets.length > 1 && (
        <div className="flex items-center gap-1.5 px-3 pt-2 pb-1 border-b border-border/60 bg-muted/30 overflow-x-auto">
          {sheets.map((sheet, idx) => (
            <button
              key={sheet.name}
              type="button"
              onClick={() => {
                setActiveSheetIndex(idx)
                setSearchTerm("")
                setSortCol(null)
              }}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
                activeSheetIndex === idx
                  ? "bg-background text-primary shadow-xs border border-border/70"
                  : "text-muted-foreground hover:text-foreground hover:bg-background/50"
              }`}
            >
              <Sheet className="h-3.5 w-3.5" />
              <span>{sheet.name}</span>
              <span className="text-[10px] opacity-60">({sheet.rows.length})</span>
            </button>
          ))}
        </div>
      )}

      {/* Control Bar */}
      <div className="p-3 border-b border-border/60 bg-muted/20 flex flex-wrap items-center justify-between gap-3 text-xs">
        <div className="flex items-center gap-2 flex-1 min-w-[200px] max-w-md">
          <div className="relative w-full">
            <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
            <Input
              type="text"
              placeholder={`Search ${currentSheet.name || "sheet"} (${currentSheet.rows.length.toLocaleString()} rows)...`}
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

        <div className="flex items-center gap-2">
          <span className="px-2.5 py-1 rounded-md bg-[#ede9de] dark:bg-[#282a2c] text-foreground/80 font-medium text-[11px]">
            {currentSheet.headers.length} Columns · {currentSheet.rows.length.toLocaleString()} Rows
          </span>
        </div>
      </div>

      {/* Grid Container with unified horizontal scroll */}
      <div className="flex-1 flex flex-col min-h-0 overflow-x-auto">
        <div style={{ minWidth: `${tableMinWidth}px`, width: "100%" }} className="flex flex-col flex-1">
          {/* Sticky Table Header */}
          <div className="flex items-center bg-muted/40 border-b border-border/70 text-[11px] font-bold text-muted-foreground uppercase tracking-wider select-none shrink-0 sticky top-0 z-10">
            <div className="w-14 shrink-0 px-3 py-3 text-center border-r border-border/50">#</div>
            <div className="flex-1 flex min-w-0">
              {currentSheet.headers.map((header, colIdx) => (
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
                <p>No rows in this sheet</p>
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
    </div>
  )
}
