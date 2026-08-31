import { useState, useMemo } from "react"
import {
  Presentation,
  ChevronLeft,
  ChevronRight,
  LayoutGrid,
  Search
} from "lucide-react"
import { Input } from "../ui/input"
import { Button } from "../ui/button"

interface PptxViewerProps {
  content?: string
  chunks?: Array<{
    id: string
    page_number?: number | null
    text: string
    section_path?: string[]
  }>
  filename?: string
}

interface SlideData {
  slideNumber: number
  title: string
  points: string[]
  rawText: string
}

function parseSlidesFromContent(
  text: string,
  chunks?: Array<{ id: string; page_number?: number | null; text: string; section_path?: string[] }>
): SlideData[] {
  if (chunks && chunks.length > 0) {
    // Group chunks by page_number (which represents slide number in PPTX ingestion)
    const slideMap = new Map<number, { points: string[]; title: string; raw: string[] }>()

    chunks.forEach((ch, idx) => {
      const slideNum = ch.page_number || idx + 1
      if (!slideMap.has(slideNum)) {
        const title = ch.section_path?.[0] || ch.text.split("\n")[0] || `Slide ${slideNum}`
        slideMap.set(slideNum, { title, points: [], raw: [] })
      }
      const item = slideMap.get(slideNum)!
      item.raw.push(ch.text)

      const lines = ch.text
        .split("\n")
        .map((l) => l.trim())
        .filter((l) => l.length > 0)
      item.points.push(...lines)
    })

    return Array.from(slideMap.entries()).map(([slideNum, data]) => ({
      slideNumber: slideNum,
      title: data.title,
      points: data.points.slice(0, 10),
      rawText: data.raw.join("\n\n")
    }))
  }

  // Fallback: split content by slide markers or double newlines
  const sections = text.split(/(?:Slide\s*\d+|---|\n{3,})/i).filter((s) => s.trim().length > 0)
  if (sections.length === 0) {
    return [
      {
        slideNumber: 1,
        title: "Presentation Content",
        points: [text || "No slide content extracted."],
        rawText: text || ""
      }
    ]
  }

  return sections.map((sec, idx) => {
    const lines = sec
      .split("\n")
      .map((l) => l.trim())
      .filter((l) => l.length > 0)
    const title = lines[0] || `Slide ${idx + 1}`
    const points = lines.slice(1)
    return {
      slideNumber: idx + 1,
      title,
      points: points.length > 0 ? points : [lines[0] || ""],
      rawText: sec
    }
  })
}

export function PptxViewer({ content = "", chunks = [], filename = "presentation.pptx" }: PptxViewerProps) {
  const [activeSlide, setActiveSlide] = useState(0)
  const [viewMode, setViewMode] = useState<"card" | "grid">("card")
  const [searchTerm, setSearchTerm] = useState("")

  const slides = useMemo(() => parseSlidesFromContent(content, chunks), [content, chunks])

  const filteredSlides = useMemo(() => {
    if (!searchTerm.trim()) return slides
    const q = searchTerm.toLowerCase()
    return slides.filter(
      (s) => s.title.toLowerCase().includes(q) || s.rawText.toLowerCase().includes(q)
    )
  }, [slides, searchTerm])

  const currentSlide = slides[activeSlide] || slides[0] || {
    slideNumber: 1,
    title: "Slide 1",
    points: [],
    rawText: ""
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
              placeholder={`Search ${slides.length} slides...`}
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="pl-8 h-8 text-xs bg-background rounded-lg border-border/60"
            />
          </div>
        </div>

        <div className="flex items-center gap-2">
          <span className="px-2.5 py-1 rounded-md bg-[#ede9de] dark:bg-[#282a2c] text-foreground/80 font-medium text-[11px]">
            {slides.length} Slides
          </span>

          <div className="flex items-center bg-background rounded-lg p-0.5 border border-border/60">
            <button
              type="button"
              onClick={() => setViewMode("card")}
              className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium transition-colors cursor-pointer ${
                viewMode === "card"
                  ? "bg-primary text-primary-foreground shadow-xs"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              <Presentation className="h-3 w-3" />
              <span>Slide View</span>
            </button>
            <button
              type="button"
              onClick={() => setViewMode("grid")}
              className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium transition-colors cursor-pointer ${
                viewMode === "grid"
                  ? "bg-primary text-primary-foreground shadow-xs"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              <LayoutGrid className="h-3 w-3" />
              <span>Grid View</span>
            </button>
          </div>
        </div>
      </div>

      {/* Main Slide Deck Area */}
      {viewMode === "card" ? (
        <div className="flex-1 flex flex-col justify-between p-6 md:p-10 bg-background/40 overflow-y-auto">
          {/* Active Slide Card */}
          <div className="w-full max-w-4xl mx-auto aspect-[16/9] min-h-[360px] bg-card border-2 border-border/80 rounded-3xl p-8 md:p-12 shadow-lg flex flex-col justify-between relative">
            <div className="space-y-4">
              <div className="flex items-center justify-between pb-3 border-b border-border/50">
                <span className="text-xs font-bold text-primary tracking-widest uppercase font-mono">
                  Slide {currentSlide.slideNumber} of {slides.length}
                </span>
                <Presentation className="h-5 w-5 text-primary opacity-60" />
              </div>

              <h2 className="font-headline text-2xl md:text-3xl font-bold text-foreground">
                {currentSlide.title}
              </h2>

              <ul className="space-y-3 pt-3">
                {currentSlide.points.map((point, pIdx) => (
                  <li key={pIdx} className="flex items-start gap-3 text-sm md:text-base text-foreground/90 leading-relaxed">
                    <span className="h-2 w-2 rounded-full bg-primary mt-2 shrink-0" />
                    <span>{point}</span>
                  </li>
                ))}
              </ul>
            </div>

            <div className="pt-4 border-t border-border/40 text-[11px] text-muted-foreground flex items-center justify-between font-mono">
              <span>{filename}</span>
              <span>KRE Slide Deck Engine</span>
            </div>
          </div>

          {/* Navigation Controls */}
          <div className="flex items-center justify-center gap-3 pt-6">
            <Button
              type="button"
              variant="outline"
              disabled={activeSlide <= 0}
              onClick={() => setActiveSlide((s) => Math.max(0, s - 1))}
              className="h-9 px-4 rounded-xl cursor-pointer"
            >
              <ChevronLeft className="h-4 w-4 mr-1" />
              <span>Previous Slide</span>
            </Button>

            <span className="text-xs font-semibold px-3 py-1.5 rounded-lg bg-muted text-foreground">
              {activeSlide + 1} / {slides.length}
            </span>

            <Button
              type="button"
              variant="outline"
              disabled={activeSlide >= slides.length - 1}
              onClick={() => setActiveSlide((s) => Math.min(slides.length - 1, s + 1))}
              className="h-9 px-4 rounded-xl cursor-pointer"
            >
              <span>Next Slide</span>
              <ChevronRight className="h-4 w-4 ml-1" />
            </Button>
          </div>
        </div>
      ) : (
        /* Slide Deck Grid */
        <div className="flex-1 p-6 md:p-8 overflow-y-auto grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {filteredSlides.map((slide, idx) => (
            <div
              key={slide.slideNumber}
              onClick={() => {
                setActiveSlide(idx)
                setViewMode("card")
              }}
              className="aspect-[16/9] rounded-2xl border border-border/80 bg-card hover:border-primary p-5 flex flex-col justify-between shadow-xs hover:shadow-md cursor-pointer group transition-all"
            >
              <div className="space-y-2">
                <span className="text-[10px] font-bold text-primary font-mono uppercase">
                  Slide {slide.slideNumber}
                </span>
                <h3 className="font-headline font-bold text-sm text-foreground group-hover:text-primary transition-colors line-clamp-2">
                  {slide.title}
                </h3>
                <p className="text-xs text-muted-foreground line-clamp-3 leading-relaxed">
                  {slide.points.join(" ")}
                </p>
              </div>
              <span className="text-[10px] text-muted-foreground pt-2 border-t border-border/40">
                Click to inspect slide
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
