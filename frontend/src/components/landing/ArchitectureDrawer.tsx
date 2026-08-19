import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription
} from "../ui/dialog"
import { Button } from "../ui/button"
import { ShieldCheck, CheckCircle2, ArrowRight } from "lucide-react"
import { useNavigate } from "react-router-dom"

interface ArchitectureDrawerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function ArchitectureDrawer({ open, onOpenChange }: ArchitectureDrawerProps) {
  const navigate = useNavigate()

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-4xl max-h-[90vh] overflow-y-auto bg-[#faf9f5] text-[#141413] border border-[#e6dfd8] shadow-2xl rounded-2xl p-6 sm:p-10 font-sans">
        <DialogHeader className="space-y-2 mb-6 text-left border-b border-[#e6dfd8] pb-4">
          <div className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-[#cc785c]/10 text-[#cc785c] text-xs font-semibold uppercase tracking-wider w-fit">
            Deterministic Engine Architecture
          </div>
          <DialogTitle className="font-serif text-2xl sm:text-3xl text-[#141413]">
            How KRE Guarantees Zero Hallucinations
          </DialogTitle>
          <DialogDescription className="text-sm text-[#56423c] leading-relaxed">
            Standard RAG dumps chopped text chunks into a probabilistic LLM. KRE uses a deterministic 4-stage verification pipeline with bounding box citation grounding.
          </DialogDescription>
        </DialogHeader>

        {/* 4 Pipeline Stages */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 sm:gap-6 my-4">
          {/* Stage 1 */}
          <div className="p-5 bg-white/90 rounded-2xl border border-[#e6dfd8] space-y-3">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-xl bg-[#cc785c]/10 text-[#cc785c] flex items-center justify-center font-serif font-bold text-base">
                1
              </div>
              <div>
                <h4 className="font-semibold text-sm text-[#141413]">Structural PageIndex Ingestion</h4>
                <p className="text-xs text-[#89726b]">Hierarchical document tree parsing</p>
              </div>
            </div>
            <p className="text-xs text-[#56423c] leading-relaxed">
              PDFs, XLSX spreadsheets, and DOCX files are parsed with their native geometry preserved. Every paragraph, table cell, and heading retains its precise pixel bounding box coordinates <code className="px-1 py-0.5 bg-[#efe9de] rounded text-[11px] text-[#141413]">[x, y, w, h]</code>.
            </p>
          </div>

          {/* Stage 2 */}
          <div className="p-5 bg-white/90 rounded-2xl border border-[#e6dfd8] space-y-3">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-xl bg-[#cc785c]/10 text-[#cc785c] flex items-center justify-center font-serif font-bold text-base">
                2
              </div>
              <div>
                <h4 className="font-semibold text-sm text-[#141413]">Zero-LLM Fast Path</h4>
                <p className="text-xs text-[#89726b]">Sub-200ms deterministic resolution</p>
              </div>
            </div>
            <p className="text-xs text-[#56423c] leading-relaxed">
              Over 88% of factual lookup queries are resolved via deterministic indexed lookup without triggering external LLM APIs. This eliminates 80%+ of API compute costs while achieving sub-2-second latency.
            </p>
          </div>

          {/* Stage 3 */}
          <div className="p-5 bg-white/90 rounded-2xl border border-[#e6dfd8] space-y-3">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-xl bg-[#cc785c]/10 text-[#cc785c] flex items-center justify-center font-serif font-bold text-base">
                3
              </div>
              <div>
                <h4 className="font-semibold text-sm text-[#141413]">Deterministic Fidelity Guardrail</h4>
                <p className="text-xs text-[#89726b]">Guaranteed NOT_FOUND enforcement</p>
              </div>
            </div>
            <p className="text-xs text-[#56423c] leading-relaxed">
              Before generation, a fidelity gate validates that candidate contexts strictly contain the factual predicate. If relevance score is below the threshold, the system enforces a certified <code className="px-1 py-0.5 bg-[#efe9de] rounded text-[11px] font-semibold text-[#c96442]">NOT_FOUND</code> response.
            </p>
          </div>

          {/* Stage 4 */}
          <div className="p-5 bg-white/90 rounded-2xl border border-[#e6dfd8] space-y-3">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-xl bg-[#cc785c]/10 text-[#cc785c] flex items-center justify-center font-serif font-bold text-base">
                4
              </div>
              <div>
                <h4 className="font-semibold text-sm text-[#141413]">Auditable Bounding Box Citations</h4>
                <p className="text-xs text-[#89726b]">1-click visual paragraph verification</p>
              </div>
            </div>
            <p className="text-xs text-[#56423c] leading-relaxed">
              Every sentence in the final output is bound to interactive citation tokens <code className="px-1.5 py-0.5 bg-[#cc785c]/15 text-[#cc785c] rounded text-[11px] font-semibold">[1]</code> that directly focus the PDF viewer to the corresponding bounding box overlay in under 300ms.
            </p>
          </div>
        </div>

        {/* API Response JSON Sample */}
        <div className="mt-6 p-5 bg-[#181715] rounded-2xl border border-[#2d2b27] text-left space-y-2">
          <div className="flex items-center justify-between text-xs text-[#c4c7c5] pb-2 border-b border-[#2d2b27]">
            <span className="font-mono text-[#cc785c]">KRE Verified API Response</span>
            <span className="flex items-center gap-1.5 text-[11px] text-[#6cd7d8]">
              <CheckCircle2 className="w-3.5 h-3.5" /> 100% Deterministic Grounding
            </span>
          </div>
          <pre className="text-xs font-mono text-[#e2e2e5] overflow-x-auto p-2 leading-relaxed">
{`{
  "answer": "The aggregate liability cap under Section 14.2 is capped at $5,000,000 [1].",
  "confidence": 0.994,
  "execution_mode": "FAST_PATH_VERIFIED",
  "latency_ms": 1420,
  "citations": [
    {
      "id": 1,
      "document_id": "doc_msa_v4_2026.pdf",
      "page_number": 47,
      "bounding_box": { "x": 142.5, "y": 384.0, "width": 420.0, "height": 64.0 },
      "exact_quote": "Section 14.2: Maximum cumulative liability shall not exceed five million USD ($5,000,000)."
    }
  ]
}`}
          </pre>
        </div>

        <div className="mt-8 flex flex-col sm:flex-row items-center justify-between gap-4 pt-4 border-t border-[#e6dfd8]">
          <div className="flex items-center gap-2 text-xs text-[#56423c]">
            <ShieldCheck className="w-4 h-4 text-[#cc785c]" />
            <span>Certified for HIPAA, SOC2 Type II, FINRA & SEC 17a-4</span>
          </div>
          <Button
            onClick={() => {
              onOpenChange(false)
              navigate("/workspaces")
            }}
            className="w-full sm:w-auto bg-[#cc785c] hover:bg-[#b56449] text-white font-medium rounded-xl text-xs h-10 px-5 shadow-xs transition-colors"
          >
            Explore Live in Workspace <ArrowRight className="w-4 h-4 ml-1.5" />
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
