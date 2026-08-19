import { useState } from "react"
import { useNavigate, Link } from "react-router-dom"
import {
  Shield,
  Clock,
  TrendingUp,
  Check,
  X,
  ArrowRight,
  Sparkles,
  Lock,
  ChevronRight,
  Database,
  Eye,
  CheckCircle2,
  AlertTriangle,
  Zap,
  BarChart3,
  ShieldCheck
} from "lucide-react"
import { Button } from "../components/ui/button"
import { EnterpriseDemoModal } from "../components/landing/EnterpriseDemoModal"
import { ArchitectureDrawer } from "../components/landing/ArchitectureDrawer"
import { ThemeToggle } from "../components/common/ThemeToggle"
import { useAuthStore } from "../store/useAuthStore"
import { toast } from "sonner"

export function LandingPage() {
  const navigate = useNavigate()
  const { login } = useAuthStore()
  const [demoOpen, setDemoOpen] = useState(false)
  const [archOpen, setArchOpen] = useState(false)
  const [activeCitation, setActiveCitation] = useState<number | null>(1)

  // Direct dummy login & workspace transition
  const handleQuickWorkspaceAccess = async () => {
    toast.info("Entering KRE Live Workspace...")
    await login({ email: "alexandra.chen@enterprise.com", password: "demo", remember_me: true })
    navigate("/workspaces")
  }

  return (
    <div className="min-h-screen bg-[#faf9f5] text-[#141413] font-sans antialiased selection:bg-[#cc785c]/20 selection:text-[#141413]">
      {/* ------------------------------------------------------------- */}
      {/* 1. NAV BAR (Sticky, Cream Background)                         */}
      {/* ------------------------------------------------------------- */}
      <header className="sticky top-0 z-50 w-full bg-[#faf9f5]/90 backdrop-blur-md border-b border-[#e6dfd8] transition-all">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-20 flex items-center justify-between">
          {/* Left: Wordmark */}
          <div className="flex items-center gap-3">
            <Link to="/" className="group flex items-center gap-2.5">
              <div className="w-9 h-9 rounded-xl bg-[#cc785c] text-white flex items-center justify-center font-serif font-bold text-lg shadow-xs group-hover:bg-[#b56449] transition-colors">
                K
              </div>
              <div className="flex flex-col">
                <span className="font-serif text-2xl font-bold tracking-tight text-[#141413]">
                  KRE
                </span>
                <span className="text-[10px] font-semibold tracking-widest text-[#89726b] uppercase -mt-1">
                  Retrieval Engine
                </span>
              </div>
            </Link>
          </div>

          {/* Center: Navigation Links */}
          <nav className="hidden md:flex items-center gap-8 text-sm font-medium text-[#56423c]">
            <a href="#platform" className="hover:text-[#141413] transition-colors">
              Platform
            </a>
            <a href="#why-kre" className="hover:text-[#141413] transition-colors">
              Why KRE
            </a>
            <a href="#benchmarks" className="hover:text-[#141413] transition-colors">
              Benchmarks
            </a>
            <a href="#security" className="hover:text-[#141413] transition-colors">
              Security
            </a>
          </nav>

          {/* Right: Actions */}
          <div className="flex items-center gap-3 sm:gap-4">
            <div className="hidden sm:block">
              <ThemeToggle />
            </div>

            <Link
              to="/login"
              className="text-sm font-medium text-[#56423c] hover:text-[#141413] transition-colors px-2 py-1"
            >
              Sign In
            </Link>

            <Button
              onClick={() => setDemoOpen(true)}
              className="bg-[#cc785c] hover:bg-[#b56449] text-white font-medium text-xs sm:text-sm rounded-xl px-4 sm:px-5 h-10 shadow-xs transition-all hover:shadow-md"
            >
              Request Enterprise Demo
            </Button>
          </div>
        </div>
      </header>

      <main>
        {/* ------------------------------------------------------------- */}
        {/* 2. HERO SECTION (The Hook: 60/40 Split)                       */}
        {/* ------------------------------------------------------------- */}
        <section className="relative pt-12 pb-20 sm:pt-16 sm:pb-28 border-b border-[#e6dfd8] overflow-hidden">
          {/* Subtle warm decorative background light */}
          <div className="absolute top-0 left-1/2 -translate-x-1/2 w-full max-w-7xl h-96 bg-radial from-[#efe9de]/60 to-transparent pointer-events-none -z-10" />

          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-12 lg:gap-8 items-center">
              {/* Left 60% (lg:col-span-7) */}
              <div className="lg:col-span-7 space-y-6 text-left">
                {/* Pill Tag */}
                <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-[#efe9de] border border-[#e6dfd8] text-xs font-semibold text-[#56423c]">
                  <span className="w-2 h-2 rounded-full bg-[#cc785c] animate-pulse" />
                  <span>Enterprise Document Intelligence</span>
                  <span className="text-[#89726b]">•</span>
                  <span className="text-[#cc785c]">Deterministic Verification</span>
                </div>

                {/* H1 Serif 64px */}
                <h1 className="font-serif text-4xl sm:text-5xl lg:text-[62px] font-bold text-[#141413] leading-[1.08] tracking-tight">
                  Verifiable intelligence for regulated industries.
                </h1>

                {/* Subtext 18px */}
                <p className="text-base sm:text-lg lg:text-[19px] text-[#56423c] leading-relaxed max-w-2xl font-sans">
                  ChatGPT hallucinates. Search engines lack context. KRE is the enterprise document intelligence platform that traces every answer to an exact page, paragraph, or cell. Zero hallucinations. Total compliance.
                </p>

                {/* CTA Row */}
                <div className="pt-2 flex flex-col sm:flex-row items-stretch sm:items-center gap-4">
                  <Button
                    onClick={() => setDemoOpen(true)}
                    className="bg-[#cc785c] hover:bg-[#b56449] text-white font-medium text-base rounded-xl h-12 px-7 shadow-sm transition-all hover:shadow-md"
                  >
                    Request Enterprise Demo
                  </Button>

                  <button
                    onClick={() => setArchOpen(true)}
                    className="inline-flex items-center justify-center gap-2 text-sm font-semibold text-[#141413] hover:text-[#cc785c] transition-colors py-2 px-3 group"
                  >
                    <span>See the Architecture</span>
                    <ArrowRight className="w-4 h-4 group-hover:translate-x-1 transition-transform" />
                  </button>
                </div>

                {/* Live Workspace Jump Pill */}
                <div className="pt-4 flex items-center gap-3">
                  <button
                    onClick={handleQuickWorkspaceAccess}
                    className="inline-flex items-center gap-2 text-xs font-medium text-[#56423c] bg-[#efe9de]/70 hover:bg-[#efe9de] border border-[#e6dfd8] rounded-lg px-3 py-1.5 transition-colors"
                  >
                    <span className="w-1.5 h-1.5 rounded-full bg-emerald-600" />
                    <span>Instant Workspace Preview (No setup required)</span>
                    <ChevronRight className="w-3.5 h-3.5 text-[#89726b]" />
                  </button>
                </div>
              </div>

              {/* Right 40% (lg:col-span-5): 3-Pane Workspace UI Mockup */}
              <div className="lg:col-span-5">
                <div className="relative rounded-2xl bg-[#efe9de]/90 p-2 sm:p-3 border border-[#e6dfd8] shadow-xl">
                  {/* Window Bar */}
                  <div className="flex items-center justify-between px-3 py-2 border-b border-[#e6dfd8] mb-2 bg-[#faf9f5] rounded-t-xl text-[11px] text-[#89726b]">
                    <div className="flex items-center gap-1.5">
                      <span className="w-2.5 h-2.5 rounded-full bg-[#e6dfd8]" />
                      <span className="w-2.5 h-2.5 rounded-full bg-[#e6dfd8]" />
                      <span className="w-2.5 h-2.5 rounded-full bg-[#e6dfd8]" />
                    </div>
                    <span className="font-mono text-[10px] text-[#56423c] font-medium">
                      kre-workspace / sec-audit-q4
                    </span>
                    <span className="flex items-center gap-1 text-[10px] text-[#cc785c] font-semibold">
                      <Zap className="w-3 h-3" /> 1.2s FastPath
                    </span>
                  </div>

                  {/* 2-Column Mockup Area (Center Chat + Right PDF Viewer) */}
                  <div className="grid grid-cols-1 sm:grid-cols-12 gap-2 bg-[#faf9f5] rounded-xl p-3 border border-[#e6dfd8] min-h-[380px]">
                    {/* Mock Chat Pane (sm:col-span-7) */}
                    <div className="sm:col-span-7 flex flex-col justify-between border-b sm:border-b-0 sm:border-r border-[#e6dfd8] pr-0 sm:pr-3 pb-3 sm:pb-0">
                      <div className="space-y-3">
                        {/* User Question */}
                        <div className="bg-[#efe9de]/70 rounded-xl p-2.5 border border-[#e6dfd8] text-xs text-[#141413]">
                          <span className="text-[10px] uppercase font-bold tracking-wider text-[#89726b] block mb-1">
                            Query
                          </span>
                          What is the aggregate liability cap under Section 14.2?
                        </div>

                        {/* AI Answer with Citation Chips */}
                        <div className="space-y-2 text-left">
                          <div className="flex items-center gap-1.5 text-[11px] font-semibold text-[#cc785c]">
                            <CheckCircle2 className="w-3.5 h-3.5" />
                            <span>100% Grounded Answer</span>
                          </div>

                          <div className="text-xs text-[#141413] leading-relaxed bg-white/90 p-3 rounded-xl border border-[#e6dfd8] shadow-2xs">
                            Under Section 14.2, maximum aggregate liability for standard breach is strictly capped at <strong className="text-[#141413] font-semibold">$5,000,000 USD</strong>
                            <button
                              onClick={() => setActiveCitation(1)}
                              className={`inline-flex items-center mx-1 px-1.5 py-0.5 rounded text-[10px] font-bold cursor-pointer transition-all ${
                                activeCitation === 1
                                  ? "bg-[#cc785c] text-white shadow-xs"
                                  : "bg-[#cc785c]/15 text-[#cc785c] hover:bg-[#cc785c]/25"
                              }`}
                            >
                              [1]
                            </button>
                            , excluding gross negligence covenants defined in Section 14.3
                            <button
                              onClick={() => setActiveCitation(2)}
                              className={`inline-flex items-center mx-1 px-1.5 py-0.5 rounded text-[10px] font-bold cursor-pointer transition-all ${
                                activeCitation === 2
                                  ? "bg-[#cc785c] text-white shadow-xs"
                                  : "bg-[#cc785c]/15 text-[#cc785c] hover:bg-[#cc785c]/25"
                              }`}
                            >
                              [2]
                            </button>
                            .
                          </div>

                          <div className="flex items-center justify-between text-[10px] text-[#89726b] px-1">
                            <span>Confidence: <strong className="text-emerald-700">99.8%</strong></span>
                            <span>Guardrail: <strong className="text-[#cc785c]">PASSED</strong></span>
                          </div>
                        </div>
                      </div>

                      {/* Mock Input Bar */}
                      <div className="pt-3">
                        <div className="w-full bg-[#efe9de]/50 border border-[#e6dfd8] rounded-lg px-2.5 py-1.5 text-[11px] text-[#89726b] flex items-center justify-between">
                          <span>Ask a question with citation proof...</span>
                          <span className="text-[9px] bg-[#e6dfd8] text-[#56423c] px-1.5 py-0.5 rounded font-mono">⌘K</span>
                        </div>
                      </div>
                    </div>

                    {/* Mock Right PDF Viewer Pane (sm:col-span-5) */}
                    <div className="sm:col-span-5 bg-white rounded-xl p-3 border border-[#e6dfd8] flex flex-col justify-between overflow-hidden">
                      <div className="space-y-2">
                        {/* Doc header */}
                        <div className="flex items-center justify-between text-[10px] text-[#89726b] pb-1.5 border-b border-[#e6dfd8]">
                          <span className="font-mono truncate max-w-[100px]">MSA_2026.pdf</span>
                          <span className="font-semibold text-[#56423c]">Pg. 47</span>
                        </div>

                        {/* Document text content with Bounding Box highlight */}
                        <div className="text-[9.5px] text-[#89726b] leading-tight space-y-1.5 font-serif select-none pt-1">
                          <p className="line-through opacity-30 text-[8.5px]">14.1 Governing Law and Dispute Resolution provisions...</p>

                          {/* Highlighted Bounding Box */}
                          <div
                            className={`p-1.5 rounded border transition-all ${
                              activeCitation === 1
                                ? "bg-[#fdeae4] border-[#cc785c] text-[#141413] shadow-xs"
                                : "bg-[#fdeae4]/40 border-[#cc785c]/40 text-[#56423c]"
                            }`}
                          >
                            <div className="flex items-center justify-between text-[8px] font-sans font-bold text-[#cc785c] mb-0.5">
                              <span>CITATION [1] BOUNDING BOX</span>
                              <span>x:142 y:384</span>
                            </div>
                            <p className="font-medium">
                              "Section 14.2: Maximum cumulative liability shall not exceed five million USD ($5,000,000)."
                            </p>
                          </div>

                          <div
                            className={`p-1.5 rounded border transition-all ${
                              activeCitation === 2
                                ? "bg-[#fdeae4] border-[#cc785c] text-[#141413] shadow-xs"
                                : "bg-[#efe9de]/30 border-transparent text-[#89726b]"
                            }`}
                          >
                            <p>
                              "Section 14.3: Exceptions to limitations of liability for gross negligence covenants..."
                            </p>
                          </div>

                          <p className="opacity-40 text-[8.5px]">14.4 Insurance coverage & indemnification riders...</p>
                        </div>
                      </div>

                      <div className="pt-2 text-center">
                        <span className="inline-block text-[9px] text-[#cc785c] font-semibold uppercase tracking-wider bg-[#cc785c]/10 px-2 py-0.5 rounded">
                          Exact Bounding Box Grounded
                        </span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------------- */}
        {/* 3. TRUST BAR (Social Proof)                                   */}
        {/* ------------------------------------------------------------- */}
        <section className="py-10 bg-[#faf9f5] border-b border-[#e6dfd8]">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 text-center space-y-6">
            <p className="text-xs sm:text-sm font-medium uppercase tracking-[0.18em] text-[#89726b]">
              Trusted by compliance, legal, and financial leaders
            </p>

            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-6 sm:gap-8 items-center justify-center opacity-75 grayscale hover:grayscale-0 transition-all">
              {/* Enterprise Logo 1 */}
              <div className="flex items-center justify-center gap-2 font-serif font-bold text-sm sm:text-base text-[#56423c] hover:text-[#141413] transition-colors">
                <span className="w-5 h-5 rounded bg-[#56423c] text-white text-[10px] flex items-center justify-center font-sans font-bold">M</span>
                MERIDIAN CAPITAL
              </div>

              {/* Enterprise Logo 2 */}
              <div className="flex items-center justify-center gap-2 font-serif font-bold text-sm sm:text-base text-[#56423c] hover:text-[#141413] transition-colors">
                <span className="w-5 h-5 rounded bg-[#56423c] text-white text-[10px] flex items-center justify-center font-sans font-bold">N</span>
                NOVUS LEGAL
              </div>

              {/* Enterprise Logo 3 */}
              <div className="flex items-center justify-center gap-2 font-serif font-bold text-sm sm:text-base text-[#56423c] hover:text-[#141413] transition-colors">
                <span className="w-5 h-5 rounded bg-[#56423c] text-white text-[10px] flex items-center justify-center font-sans font-bold">A</span>
                APEX BIO-PHARMA
              </div>

              {/* Enterprise Logo 4 */}
              <div className="flex items-center justify-center gap-2 font-serif font-bold text-sm sm:text-base text-[#56423c] hover:text-[#141413] transition-colors">
                <span className="w-5 h-5 rounded bg-[#56423c] text-white text-[10px] flex items-center justify-center font-sans font-bold">V</span>
                VAULT ASSETS
              </div>

              {/* Enterprise Logo 5 */}
              <div className="col-span-2 sm:col-span-1 flex items-center justify-center gap-2 font-serif font-bold text-sm sm:text-base text-[#56423c] hover:text-[#141413] transition-colors">
                <span className="w-5 h-5 rounded bg-[#56423c] text-white text-[10px] flex items-center justify-center font-sans font-bold">S</span>
                SENTINEL HEALTH
              </div>
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------------- */}
        {/* 4. THE PROBLEM (Dark Section #181715)                         */}
        {/* ------------------------------------------------------------- */}
        <section id="problem" className="py-20 sm:py-28 bg-[#181715] text-[#faf9f5] border-b border-[#2d2b27]">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 space-y-12">
            <div className="text-center space-y-3 max-w-3xl mx-auto">
              <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-[#252320] border border-[#383530] text-xs font-semibold text-[#cc785c] uppercase tracking-wider">
                The Compliance Bottleneck
              </div>
              <h2 className="font-serif text-3xl sm:text-4xl lg:text-5xl font-bold text-[#faf9f5] tracking-tight">
                Generative AI fails enterprise compliance.
              </h2>
              <p className="text-sm sm:text-base text-[#c4c7c5] leading-relaxed">
                Standard vector RAG architectures are built for approximate chatbots, not rigorous legal and financial verification.
              </p>
            </div>

            {/* 3-Column Problem Grid */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-6 sm:gap-8">
              {/* Problem 1 */}
              <div className="p-8 rounded-2xl bg-[#252320] border border-[#33302b] hover:border-[#cc785c]/50 transition-all space-y-4 text-left">
                <div className="w-12 h-12 rounded-xl bg-[#181715] text-[#cc785c] flex items-center justify-center border border-[#33302b]">
                  <Eye className="w-6 h-6" />
                </div>
                <h3 className="font-serif text-2xl font-semibold text-[#faf9f5]">
                  Opaque Answers
                </h3>
                <p className="text-sm text-[#c4c7c5] leading-relaxed">
                  Vector RAG returns plausible answers with no audit trail. Compliance officers cannot verify where the LLM generated the response, making sign-off impossible.
                </p>
              </div>

              {/* Problem 2 */}
              <div className="p-8 rounded-2xl bg-[#252320] border border-[#33302b] hover:border-[#cc785c]/50 transition-all space-y-4 text-left">
                <div className="w-12 h-12 rounded-xl bg-[#181715] text-[#cc785c] flex items-center justify-center border border-[#33302b]">
                  <AlertTriangle className="w-6 h-6" />
                </div>
                <h3 className="font-serif text-2xl font-semibold text-[#faf9f5]">
                  Context Hallucinations
                </h3>
                <p className="text-sm text-[#c4c7c5] leading-relaxed">
                  When given ambiguous context, LLMs guess. In regulated industries, a single hallucinated fact or inverted clause can result in millions in fines and breach of warranty.
                </p>
              </div>

              {/* Problem 3 */}
              <div className="p-8 rounded-2xl bg-[#252320] border border-[#33302b] hover:border-[#cc785c]/50 transition-all space-y-4 text-left">
                <div className="w-12 h-12 rounded-xl bg-[#181715] text-[#cc785c] flex items-center justify-center border border-[#33302b]">
                  <Database className="w-6 h-6" />
                </div>
                <h3 className="font-serif text-2xl font-semibold text-[#faf9f5]">
                  Data Sprawl
                </h3>
                <p className="text-sm text-[#c4c7c5] leading-relaxed">
                  Critical knowledge is locked across PDFs, complex spreadsheets, and legacy DOCX files. Naive chunking destroys multi-column tables and cross-page references.
                </p>
              </div>
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------------- */}
        {/* 5. THE SOLUTION (Cream Background)                            */}
        {/* ------------------------------------------------------------- */}
        <section id="solution" className="py-20 sm:py-28 bg-[#faf9f5] border-b border-[#e6dfd8]">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 space-y-20">
            {/* Header */}
            <div className="text-center space-y-3 max-w-3xl mx-auto">
              <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-[#efe9de] border border-[#e6dfd8] text-xs font-semibold text-[#cc785c] uppercase tracking-wider">
                Auditable Grounding Engine
              </div>
              <h2 className="font-serif text-3xl sm:text-4xl lg:text-5xl font-bold text-[#141413] tracking-tight">
                Every answer is an auditable fact.
              </h2>
              <p className="text-base text-[#56423c] leading-relaxed">
                KRE combines structural document geometry parsing with pre-LLM mathematical fidelity gates.
              </p>
            </div>

            {/* Alternating Row 1: Image Left, Text Right */}
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-10 lg:gap-14 items-center">
              {/* Visual Box Left */}
              <div className="lg:col-span-6 order-2 lg:order-1">
                <div className="p-6 sm:p-8 bg-[#efe9de] rounded-3xl border border-[#e6dfd8] shadow-sm space-y-4 text-left">
                  <div className="flex items-center justify-between border-b border-[#e6dfd8] pb-3 text-xs text-[#56423c]">
                    <span className="font-semibold text-[#141413]">PageIndex Coordinate Overlay</span>
                    <span className="font-mono text-[#cc785c] font-medium">3-Second SLA</span>
                  </div>

                  <div className="bg-white p-5 rounded-2xl border border-[#e6dfd8] space-y-3 shadow-2xs">
                    <div className="flex items-center justify-between text-xs text-[#89726b]">
                      <span className="font-mono font-medium text-[#141413]">Regulatory_Filing_2026.pdf</span>
                      <span className="bg-[#cc785c]/10 text-[#cc785c] font-bold px-2 py-0.5 rounded text-[11px]">
                        Page 12 • Paragraph 4
                      </span>
                    </div>

                    <div className="p-3 bg-[#fdeae4] border-2 border-[#cc785c] rounded-xl text-xs text-[#141413] font-medium leading-relaxed">
                      <div className="flex items-center justify-between text-[10px] text-[#cc785c] font-bold uppercase mb-1">
                        <span>✓ Bounding Box Match [x: 88, y: 520, w: 480, h: 72]</span>
                        <span>100% Grounded</span>
                      </div>
                      "The company maintains cash reserves exceeding the statutory minimum ratio of 14.5% across all Tier 1 subsidiaries."
                    </div>

                    <div className="flex items-center gap-4 text-[11px] text-[#89726b] pt-1">
                      <span>OCR Vector: <strong className="text-[#141413]">Normalized</strong></span>
                      <span>Latency: <strong className="text-[#141413]">240ms</strong></span>
                      <span>Target: <strong className="text-emerald-700">Cell/Box Exact</strong></span>
                    </div>
                  </div>
                </div>
              </div>

              {/* Text Right */}
              <div className="lg:col-span-6 order-1 lg:order-2 space-y-4 text-left">
                <div className="w-10 h-10 rounded-xl bg-[#cc785c]/10 text-[#cc785c] flex items-center justify-center font-bold font-serif">
                  01
                </div>
                <h3 className="font-serif text-3xl font-bold text-[#141413]">
                  Citation-to-Bounding-Box in 3 seconds.
                </h3>
                <p className="text-base text-[#56423c] leading-relaxed">
                  KRE doesn’t just tell you the answer; it shows you exactly where it is. Click a citation, and the document viewer instantly highlights the exact bounding box or spreadsheet cell. Verification takes seconds, not hours.
                </p>
                <ul className="space-y-2 pt-2 text-sm text-[#56423c]">
                  <li className="flex items-center gap-2">
                    <Check className="w-4 h-4 text-[#cc785c]" />
                    <span>Exact coordinate overlays for PDFs, scans, and spreadsheets</span>
                  </li>
                  <li className="flex items-center gap-2">
                    <Check className="w-4 h-4 text-[#cc785c]" />
                    <span>Side-by-side split screen document verification pane</span>
                  </li>
                  <li className="flex items-center gap-2">
                    <Check className="w-4 h-4 text-[#cc785c]" />
                    <span>Direct page and cell anchor links for team collaboration</span>
                  </li>
                </ul>
              </div>
            </div>

            {/* Alternating Row 2: Text Left, Image Right */}
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-10 lg:gap-14 items-center">
              {/* Text Left */}
              <div className="lg:col-span-6 space-y-4 text-left">
                <div className="w-10 h-10 rounded-xl bg-[#cc785c]/10 text-[#cc785c] flex items-center justify-center font-bold font-serif">
                  02
                </div>
                <h3 className="font-serif text-3xl font-bold text-[#141413]">
                  Zero-Hallucination Guardrails.
                </h3>
                <p className="text-base text-[#56423c] leading-relaxed">
                  Our deterministic fidelity check runs before the LLM is ever invoked. If the context doesn't perfectly support the query, KRE safely returns 'NOT_FOUND' rather than guessing. Your trust is worth more than a fake answer.
                </p>
                <ul className="space-y-2 pt-2 text-sm text-[#56423c]">
                  <li className="flex items-center gap-2">
                    <Check className="w-4 h-4 text-[#cc785c]" />
                    <span>Pre-LLM mathematical fidelity gate eliminates speculative drift</span>
                  </li>
                  <li className="flex items-center gap-2">
                    <Check className="w-4 h-4 text-[#cc785c]" />
                    <span>Strictly enforced NOT_FOUND on ambiguous contexts</span>
                  </li>
                  <li className="flex items-center gap-2">
                    <Check className="w-4 h-4 text-[#cc785c]" />
                    <span>Immutable audit log for regulatory compliance submissions</span>
                  </li>
                </ul>
              </div>

              {/* Visual Box Right */}
              <div className="lg:col-span-6">
                <div className="p-6 sm:p-8 bg-[#efe9de] rounded-3xl border border-[#e6dfd8] shadow-sm space-y-4 text-left">
                  <div className="flex items-center justify-between border-b border-[#e6dfd8] pb-3 text-xs text-[#56423c]">
                    <span className="font-semibold text-[#141413]">Fidelity Guardrail Pipeline</span>
                    <span className="font-mono text-emerald-700 font-semibold">100% Certified</span>
                  </div>

                  <div className="space-y-2">
                    <div className="p-3 bg-white rounded-xl border border-[#e6dfd8] flex items-center justify-between text-xs">
                      <span className="font-medium text-[#141413]">1. Query Predicate Extraction</span>
                      <span className="text-emerald-700 font-mono font-bold">✓ PASS</span>
                    </div>

                    <div className="p-3 bg-white rounded-xl border border-[#e6dfd8] flex items-center justify-between text-xs">
                      <span className="font-medium text-[#141413]">2. Structural PageIndex Match</span>
                      <span className="text-emerald-700 font-mono font-bold">✓ 99.4% Sim</span>
                    </div>

                    <div className="p-3 bg-[#fdeae4] rounded-xl border border-[#cc785c] flex items-center justify-between text-xs">
                      <span className="font-semibold text-[#141413]">3. Deterministic Fidelity Gate</span>
                      <span className="text-[#cc785c] font-mono font-bold">ENFORCED</span>
                    </div>

                    <div className="p-3 bg-white rounded-xl border border-[#e6dfd8] flex items-center justify-between text-xs">
                      <span className="font-medium text-[#141413]">4. Low-Confidence Fallback</span>
                      <span className="text-[#89726b] font-mono">Safe 'NOT_FOUND'</span>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------------- */}
        {/* 6. COMPETITIVE ADVANTAGE (Cream Background, Data Table UI)    */}
        {/* ------------------------------------------------------------- */}
        <section id="why-kre" className="py-20 sm:py-28 bg-[#faf9f5] border-b border-[#e6dfd8]">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 space-y-12">
            <div className="text-center space-y-3 max-w-3xl mx-auto">
              <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-[#efe9de] border border-[#e6dfd8] text-xs font-semibold text-[#cc785c] uppercase tracking-wider">
                Enterprise Matrix
              </div>
              <h2 className="font-serif text-3xl sm:text-4xl lg:text-5xl font-bold text-[#141413] tracking-tight">
                Why enterprises choose KRE over standard RAG.
              </h2>
              <p className="text-base text-[#56423c] leading-relaxed">
                Generic AI platforms summarize without accountability. KRE delivers mathematical verification.
              </p>
            </div>

            {/* Comparison Table */}
            <div className="bg-white rounded-3xl border border-[#e6dfd8] shadow-sm overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse">
                  <thead>
                    <tr className="border-b border-[#e6dfd8] bg-[#efe9de]/50 text-xs font-semibold text-[#56423c]">
                      <th className="py-5 px-6 sm:px-8 w-2/5">Capability / Requirement</th>
                      <th className="py-5 px-4 text-center">ChatGPT + PDF</th>
                      <th className="py-5 px-4 text-center">Azure AI Search</th>
                      <th className="py-5 px-6 sm:px-8 text-center bg-[#cc785c]/10 text-[#cc785c] font-bold">
                        KRE Engine
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[#e6dfd8] text-sm text-[#141413]">
                    {/* Row 1 */}
                    <tr className="hover:bg-[#faf9f5] transition-colors">
                      <td className="py-4 px-6 sm:px-8 font-medium">
                        Exact Bounding Box Citations
                        <span className="block text-xs text-[#89726b] font-normal">
                          Highlights exact physical paragraph / table coordinates
                        </span>
                      </td>
                      <td className="py-4 px-4 text-center text-[#89726b]">
                        <X className="w-5 h-5 mx-auto text-[#89726b]" />
                      </td>
                      <td className="py-4 px-4 text-center text-[#89726b]">
                        <X className="w-5 h-5 mx-auto text-[#89726b]" />
                      </td>
                      <td className="py-4 px-6 sm:px-8 text-center bg-[#cc785c]/5 font-semibold text-[#cc785c]">
                        <div className="inline-flex items-center gap-1.5 text-[#cc785c] font-bold">
                          <Check className="w-5 h-5 stroke-[2.5]" />
                          <span>Yes</span>
                        </div>
                      </td>
                    </tr>

                    {/* Row 2 */}
                    <tr className="hover:bg-[#faf9f5] transition-colors">
                      <td className="py-4 px-6 sm:px-8 font-medium">
                        Structural Document Indexing (PageIndex)
                        <span className="block text-xs text-[#89726b] font-normal">
                          Maintains native document hierarchy instead of naive chunking
                        </span>
                      </td>
                      <td className="py-4 px-4 text-center text-[#89726b]">
                        <X className="w-5 h-5 mx-auto text-[#89726b]" />
                      </td>
                      <td className="py-4 px-4 text-center text-[#89726b]">
                        <X className="w-5 h-5 mx-auto text-[#89726b]" />
                      </td>
                      <td className="py-4 px-6 sm:px-8 text-center bg-[#cc785c]/5 font-semibold text-[#cc785c]">
                        <div className="inline-flex items-center gap-1.5 text-[#cc785c] font-bold">
                          <Check className="w-5 h-5 stroke-[2.5]" />
                          <span>Yes</span>
                        </div>
                      </td>
                    </tr>

                    {/* Row 3 */}
                    <tr className="hover:bg-[#faf9f5] transition-colors">
                      <td className="py-4 px-6 sm:px-8 font-medium">
                        Zero-LLM Fast Path (Cost Savings)
                        <span className="block text-xs text-[#89726b] font-normal">
                          Deterministic resolution without calling expensive LLM APIs
                        </span>
                      </td>
                      <td className="py-4 px-4 text-center text-[#89726b]">
                        <X className="w-5 h-5 mx-auto text-[#89726b]" />
                      </td>
                      <td className="py-4 px-4 text-center text-[#89726b]">
                        <X className="w-5 h-5 mx-auto text-[#89726b]" />
                      </td>
                      <td className="py-4 px-6 sm:px-8 text-center bg-[#cc785c]/5 font-semibold text-[#cc785c]">
                        <div className="inline-flex items-center gap-1.5 text-[#cc785c] font-bold">
                          <Check className="w-5 h-5 stroke-[2.5]" />
                          <span>Yes (88% Saved)</span>
                        </div>
                      </td>
                    </tr>

                    {/* Row 4 */}
                    <tr className="hover:bg-[#faf9f5] transition-colors">
                      <td className="py-4 px-6 sm:px-8 font-medium">
                        Enforced NOT_FOUND on Low Confidence
                        <span className="block text-xs text-[#89726b] font-normal">
                          Safely rejects guesses rather than hallucinating plausible fakes
                        </span>
                      </td>
                      <td className="py-4 px-4 text-center text-[#89726b]">
                        <X className="w-5 h-5 mx-auto text-[#89726b]" />
                      </td>
                      <td className="py-4 px-4 text-center text-[#89726b]">
                        <X className="w-5 h-5 mx-auto text-[#89726b]" />
                      </td>
                      <td className="py-4 px-6 sm:px-8 text-center bg-[#cc785c]/5 font-semibold text-[#cc785c]">
                        <div className="inline-flex items-center gap-1.5 text-[#cc785c] font-bold">
                          <Check className="w-5 h-5 stroke-[2.5]" />
                          <span>Yes (Guaranteed)</span>
                        </div>
                      </td>
                    </tr>

                    {/* Row 5 */}
                    <tr className="hover:bg-[#faf9f5] transition-colors">
                      <td className="py-4 px-6 sm:px-8 font-medium">
                        Multi-Format Native (PDF, XLSX, DOCX)
                        <span className="block text-xs text-[#89726b] font-normal">
                          Full preservation of spreadsheet formulas and complex multi-column layouts
                        </span>
                      </td>
                      <td className="py-4 px-4 text-center text-xs text-[#56423c]">
                        Limited (Text only)
                      </td>
                      <td className="py-4 px-4 text-center text-xs text-[#56423c]">
                        Yes (Basic chunks)
                      </td>
                      <td className="py-4 px-6 sm:px-8 text-center bg-[#cc785c]/5 font-semibold text-[#cc785c]">
                        <div className="inline-flex items-center gap-1.5 text-[#cc785c] font-bold">
                          <Check className="w-5 h-5 stroke-[2.5]" />
                          <span>Yes (Structural)</span>
                        </div>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------------- */}
        {/* 7. BENCHMARKS / PROOF (Dark Section #181715)                  */}
        {/* ------------------------------------------------------------- */}
        <section id="benchmarks" className="py-20 sm:py-28 bg-[#181715] text-[#faf9f5] border-b border-[#2d2b27]">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 space-y-12">
            <div className="text-center space-y-3 max-w-3xl mx-auto">
              <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-[#252320] border border-[#383530] text-xs font-semibold text-[#cc785c] uppercase tracking-wider">
                Production Telemetry
              </div>
              <h2 className="font-serif text-3xl sm:text-4xl lg:text-5xl font-bold text-[#faf9f5] tracking-tight">
                Provable performance. Not just marketing.
              </h2>
              <p className="text-sm sm:text-base text-[#c4c7c5] leading-relaxed">
                We benchmark every change against a 120-query test set. These are our live production metrics.
              </p>
            </div>

            {/* 4 Large KPI Cards */}
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6">
              {/* KPI 1 */}
              <div className="p-6 sm:p-8 rounded-2xl bg-[#252320] border border-[#33302b] space-y-3 text-left hover:border-[#cc785c]/50 transition-all">
                <div className="text-xs font-semibold uppercase tracking-wider text-[#89726b]">
                  p95 Latency
                </div>
                <div className="font-serif text-4xl sm:text-5xl font-bold text-[#faf9f5]">
                  1.8s
                </div>
                <div className="text-xs text-[#6cd7d8] flex items-center gap-1">
                  <CheckCircle2 className="w-3.5 h-3.5" />
                  <span>Target &lt; 4.0s (55% faster)</span>
                </div>
              </div>

              {/* KPI 2 */}
              <div className="p-6 sm:p-8 rounded-2xl bg-[#252320] border border-[#33302b] space-y-3 text-left hover:border-[#cc785c]/50 transition-all">
                <div className="text-xs font-semibold uppercase tracking-wider text-[#89726b]">
                  Recall@5
                </div>
                <div className="font-serif text-4xl sm:text-5xl font-bold text-[#faf9f5]">
                  94%
                </div>
                <div className="text-xs text-[#6cd7d8] flex items-center gap-1">
                  <CheckCircle2 className="w-3.5 h-3.5" />
                  <span>Target &gt; 85% (+9% above target)</span>
                </div>
              </div>

              {/* KPI 3 */}
              <div className="p-6 sm:p-8 rounded-2xl bg-[#252320] border border-[#33302b] space-y-3 text-left hover:border-[#cc785c]/50 transition-all">
                <div className="text-xs font-semibold uppercase tracking-wider text-[#89726b]">
                  Faithfulness
                </div>
                <div className="font-serif text-4xl sm:text-5xl font-bold text-[#faf9f5]">
                  98%
                </div>
                <div className="text-xs text-[#cc785c] flex items-center gap-1">
                  <ShieldCheck className="w-3.5 h-3.5" />
                  <span>Target 100% Zero Hallucination</span>
                </div>
              </div>

              {/* KPI 4 */}
              <div className="p-6 sm:p-8 rounded-2xl bg-[#252320] border border-[#33302b] space-y-3 text-left hover:border-[#cc785c]/50 transition-all">
                <div className="text-xs font-semibold uppercase tracking-wider text-[#89726b]">
                  LLM Activation
                </div>
                <div className="font-serif text-4xl sm:text-5xl font-bold text-[#faf9f5]">
                  12%
                </div>
                <div className="text-xs text-[#6cd7d8] flex items-center gap-1">
                  <Zap className="w-3.5 h-3.5" />
                  <span>Target &lt; 60% (88% Cost Savings)</span>
                </div>
              </div>
            </div>

            {/* Benchmarks Action */}
            <div className="pt-4 text-center">
              <Link
                to="/benchmarks"
                className="inline-flex items-center gap-2 text-xs font-semibold text-[#faf9f5] hover:text-[#cc785c] bg-[#252320] border border-[#383530] hover:border-[#cc785c] px-4 py-2.5 rounded-xl transition-all"
              >
                <BarChart3 className="w-4 h-4 text-[#cc785c]" />
                <span>View Full 120-Query Live Benchmarks Dashboard</span>
                <ArrowRight className="w-3.5 h-3.5" />
              </Link>
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------------- */}
        {/* 8. BUSINESS IMPACT (Cream Background)                         */}
        {/* ------------------------------------------------------------- */}
        <section id="impact" className="py-20 sm:py-28 bg-[#faf9f5] border-b border-[#e6dfd8]">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 space-y-12">
            <div className="text-center space-y-3 max-w-3xl mx-auto">
              <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-[#efe9de] border border-[#e6dfd8] text-xs font-semibold text-[#cc785c] uppercase tracking-wider">
                Enterprise ROI
              </div>
              <h2 className="font-serif text-3xl sm:text-4xl lg:text-5xl font-bold text-[#141413] tracking-tight">
                Turn hours of document review into seconds.
              </h2>
              <p className="text-base text-[#56423c] leading-relaxed">
                Empower legal, compliance, and risk teams with instant auditable clarity.
              </p>
            </div>

            {/* 3-Card Grid */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-6 sm:gap-8">
              {/* Impact Card 1 */}
              <div className="p-8 rounded-3xl bg-[#efe9de] border border-[#e6dfd8] space-y-4 text-left hover:border-[#cc785c]/40 transition-all shadow-xs">
                <div className="w-12 h-12 rounded-2xl bg-white text-[#cc785c] flex items-center justify-center border border-[#e6dfd8] shadow-2xs">
                  <Clock className="w-6 h-6" />
                </div>
                <h3 className="font-serif text-2xl font-bold text-[#141413]">
                  Accelerate Due Diligence
                </h3>
                <p className="text-sm text-[#56423c] leading-relaxed">
                  Audit legal contracts and financial reports 10x faster. KRE reads the document hierarchy so you don't have to manually flip through hundreds of pages.
                </p>
              </div>

              {/* Impact Card 2 */}
              <div className="p-8 rounded-3xl bg-[#efe9de] border border-[#e6dfd8] space-y-4 text-left hover:border-[#cc785c]/40 transition-all shadow-xs">
                <div className="w-12 h-12 rounded-2xl bg-white text-[#cc785c] flex items-center justify-center border border-[#e6dfd8] shadow-2xs">
                  <Shield className="w-6 h-6" />
                </div>
                <h3 className="font-serif text-2xl font-bold text-[#141413]">
                  Eliminate Compliance Risk
                </h3>
                <p className="text-sm text-[#56423c] leading-relaxed">
                  Ensure every claim your team makes is 100% grounded in your source documents. Zero hallucinations, zero regulatory fines, and full audit accountability.
                </p>
              </div>

              {/* Impact Card 3 */}
              <div className="p-8 rounded-3xl bg-[#efe9de] border border-[#e6dfd8] space-y-4 text-left hover:border-[#cc785c]/40 transition-all shadow-xs">
                <div className="w-12 h-12 rounded-2xl bg-white text-[#cc785c] flex items-center justify-center border border-[#e6dfd8] shadow-2xs">
                  <TrendingUp className="w-6 h-6" />
                </div>
                <h3 className="font-serif text-2xl font-bold text-[#141413]">
                  Slash LLM API Costs
                </h3>
                <p className="text-sm text-[#56423c] leading-relaxed">
                  Our Fast Path handles simple factual queries with zero LLM calls. Only 12% of queries trigger the expensive LLM pipeline, saving over 80% on compute overhead.
                </p>
              </div>
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------------- */}
        {/* 9. FINAL CTA (Cream Background, Generous Whitespace)          */}
        {/* ------------------------------------------------------------- */}
        <section className="py-24 sm:py-32 bg-[#faf9f5] border-b border-[#e6dfd8] text-center">
          <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 space-y-8">
            <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-[#efe9de] border border-[#e6dfd8] text-xs font-semibold text-[#56423c]">
              <Sparkles className="w-3.5 h-3.5 text-[#cc785c]" />
              <span>Enterprise Ready • SOC2 Type II Certified</span>
            </div>

            {/* H2 Serif 56px */}
            <h2 className="font-serif text-4xl sm:text-5xl lg:text-6xl font-bold text-[#141413] tracking-tight leading-[1.1]">
              Deploy trust at scale.
            </h2>

            {/* Subtext */}
            <p className="text-base sm:text-xl text-[#56423c] max-w-2xl mx-auto leading-relaxed">
              Stop relying on black-box AI. Give your team an intelligence engine they can actually verify.
            </p>

            {/* Action buttons */}
            <div className="pt-2 flex flex-col sm:flex-row items-center justify-center gap-4">
              <Button
                onClick={() => setDemoOpen(true)}
                className="w-full sm:w-auto bg-[#cc785c] hover:bg-[#b56449] text-white font-medium text-base rounded-xl h-12 px-8 shadow-sm transition-all hover:shadow-md"
              >
                Request Enterprise Demo
              </Button>

              <Button
                onClick={handleQuickWorkspaceAccess}
                variant="outline"
                className="w-full sm:w-auto bg-transparent border-[#e6dfd8] text-[#141413] hover:bg-[#efe9de] rounded-xl h-12 px-6 text-sm font-semibold"
              >
                Launch Live Workspace <ArrowRight className="w-4 h-4 ml-2" />
              </Button>
            </div>

            <div className="pt-6 flex flex-wrap items-center justify-center gap-6 text-xs text-[#89726b]">
              <span className="flex items-center gap-1.5">
                <Lock className="w-3.5 h-3.5 text-[#cc785c]" /> On-Prem & VPC Deployment
              </span>
              <span className="flex items-center gap-1.5">
                <ShieldCheck className="w-3.5 h-3.5 text-[#cc785c]" /> HIPAA & FINRA Compliant
              </span>
              <span className="flex items-center gap-1.5">
                <Zap className="w-3.5 h-3.5 text-[#cc785c]" /> 2-Hour Setup SLA
              </span>
            </div>
          </div>
        </section>
      </main>

      {/* ------------------------------------------------------------- */}
      {/* 10. FOOTER (Dark Section #181715)                             */}
      {/* ------------------------------------------------------------- */}
      <footer id="security" className="bg-[#181715] text-[#faf9f5] pt-16 pb-12 border-t border-[#2d2b27]">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 space-y-12">
          {/* 3-Column Layout */}
          <div className="grid grid-cols-1 md:grid-cols-12 gap-8 lg:gap-12">
            {/* Col 1 (md:col-span-5): Wordmark & Description */}
            <div className="md:col-span-5 space-y-4 text-left">
              <div className="flex items-center gap-2.5">
                <div className="w-8 h-8 rounded-lg bg-[#cc785c] text-white flex items-center justify-center font-serif font-bold text-base">
                  K
                </div>
                <span className="font-serif text-2xl font-bold tracking-tight text-[#faf9f5]">
                  KRE
                </span>
              </div>
              <p className="text-xs text-[#c4c7c5] leading-relaxed max-w-sm">
                Enterprise document intelligence. Deterministic grounding and bounding box citation verification for regulated industries.
              </p>
              <div className="flex items-center gap-3 text-xs text-[#89726b] pt-2">
                <span className="px-2 py-1 bg-[#252320] border border-[#33302b] rounded-md text-[11px] text-[#c4c7c5]">
                  SOC2 Type II
                </span>
                <span className="px-2 py-1 bg-[#252320] border border-[#33302b] rounded-md text-[11px] text-[#c4c7c5]">
                  HIPAA Ready
                </span>
                <span className="px-2 py-1 bg-[#252320] border border-[#33302b] rounded-md text-[11px] text-[#c4c7c5]">
                  ISO 27001
                </span>
              </div>
            </div>

            {/* Col 2 (md:col-span-3): Product Links */}
            <div className="md:col-span-3 space-y-3 text-left">
              <div className="text-xs font-bold uppercase tracking-wider text-[#faf9f5]">
                Platform
              </div>
              <ul className="space-y-2 text-xs text-[#c4c7c5]">
                <li>
                  <a href="#platform" className="hover:text-[#faf9f5] transition-colors">
                    Platform Overview
                  </a>
                </li>
                <li>
                  <button
                    onClick={() => setArchOpen(true)}
                    className="hover:text-[#faf9f5] transition-colors text-left"
                  >
                    Engine Architecture
                  </button>
                </li>
                <li>
                  <Link to="/benchmarks" className="hover:text-[#faf9f5] transition-colors">
                    120-Query Benchmarks
                  </Link>
                </li>
                <li>
                  <button
                    onClick={handleQuickWorkspaceAccess}
                    className="hover:text-[#faf9f5] transition-colors text-left"
                  >
                    Interactive Workspace
                  </button>
                </li>
                <li>
                  <Link to="/workspaces" className="hover:text-[#faf9f5] transition-colors">
                    Document Library
                  </Link>
                </li>
              </ul>
            </div>

            {/* Col 3 (md:col-span-4): Security & Compliance */}
            <div className="md:col-span-4 space-y-3 text-left">
              <div className="text-xs font-bold uppercase tracking-wider text-[#faf9f5]">
                Security & Trust
              </div>
              <ul className="space-y-2 text-xs text-[#c4c7c5]">
                <li className="flex items-center gap-2">
                  <ShieldCheck className="w-3.5 h-3.5 text-[#cc785c]" />
                  <span>Zero Data Retention for Model Training</span>
                </li>
                <li className="flex items-center gap-2">
                  <Lock className="w-3.5 h-3.5 text-[#cc785c]" />
                  <span>AES-256 at rest / TLS 1.3 in transit</span>
                </li>
                <li>
                  <button
                    onClick={() => setDemoOpen(true)}
                    className="text-[#cc785c] hover:underline font-medium"
                  >
                    Request Security Whitepaper & Audit Packet →
                  </button>
                </li>
              </ul>
            </div>
          </div>

          {/* Bottom strip: Copyright */}
          <div className="pt-8 border-t border-[#2d2b27] flex flex-col sm:flex-row items-center justify-between gap-4 text-[11px] text-[#89726b]">
            <div>
              © 2026 KRE (Knowledge Retrieval Engine) Inc. All rights reserved.
            </div>
            <div className="flex items-center gap-6">
              <span className="hover:text-[#c4c7c5] cursor-pointer">Privacy Notice</span>
              <span className="hover:text-[#c4c7c5] cursor-pointer">Terms of Service</span>
              <span className="hover:text-[#c4c7c5] cursor-pointer">Security Center</span>
            </div>
          </div>
        </div>
      </footer>

      {/* Interactive Modals */}
      <EnterpriseDemoModal open={demoOpen} onOpenChange={setDemoOpen} />
      <ArchitectureDrawer open={archOpen} onOpenChange={setArchOpen} />
    </div>
  )
}

export default LandingPage
