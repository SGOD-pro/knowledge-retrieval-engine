import React, { useState } from "react"
import { useNavigate } from "react-router-dom"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription
} from "../ui/dialog"
import { Button } from "../ui/button"
import { Input } from "../ui/input"
import { Label } from "../ui/label"
import { toast } from "sonner"
import { CheckCircle2, Building2, ShieldCheck, ArrowRight, Sparkles } from "lucide-react"

interface EnterpriseDemoModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function EnterpriseDemoModal({ open, onOpenChange }: EnterpriseDemoModalProps) {
  const navigate = useNavigate()
  const [isSubmitted, setIsSubmitted] = useState(false)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [formData, setFormData] = useState({
    name: "",
    email: "",
    company: "",
    industry: "Legal & Compliance",
    docVolume: "10k - 100k pages/mo",
    note: ""
  })

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!formData.name || !formData.email || !formData.company) {
      toast.error("Please fill in your name, work email, and company.")
      return
    }

    setIsSubmitting(true)
    setTimeout(() => {
      setIsSubmitting(false)
      setIsSubmitted(true)
      toast.success("Demo request received! Our solution architect will reach out shortly.")
    }, 600)
  }

  const handleReset = () => {
    setIsSubmitted(false)
    setFormData({
      name: "",
      email: "",
      company: "",
      industry: "Legal & Compliance",
      docVolume: "10k - 100k pages/mo",
      note: ""
    })
  }

  const handleGoToWorkspace = () => {
    onOpenChange(false)
    navigate("/workspaces")
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(val) => {
        onOpenChange(val)
        if (!val) {
          setTimeout(handleReset, 300)
        }
      }}
    >
      <DialogContent className="max-w-lg bg-[#faf9f5] text-[#141413] border border-[#e6dfd8] shadow-2xl rounded-2xl p-6 sm:p-8 font-sans">
        {!isSubmitted ? (
          <div>
            <DialogHeader className="space-y-2 mb-6 text-left">
              <div className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-[#cc785c]/10 text-[#cc785c] text-xs font-medium w-fit">
                <Sparkles className="w-3.5 h-3.5" />
                <span>Enterprise Dedicated Pilot</span>
              </div>
              <DialogTitle className="font-serif text-2xl sm:text-3xl text-[#141413] tracking-tight">
                Request an Enterprise Demo
              </DialogTitle>
              <DialogDescription className="text-sm text-[#56423c] leading-relaxed">
                Experience deterministic citation-to-bounding-box retrieval on your actual regulated documents with SOC2 Type II compliance.
              </DialogDescription>
            </DialogHeader>

            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div className="space-y-1.5">
                  <Label htmlFor="name" className="text-xs font-semibold uppercase tracking-wider text-[#56423c]">
                    Full Name *
                  </Label>
                  <Input
                    id="name"
                    required
                    placeholder="Alexandra Chen"
                    value={formData.name}
                    onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                    className="bg-white/80 border-[#e6dfd8] focus-visible:ring-[#cc785c] rounded-xl text-sm h-10"
                  />
                </div>

                <div className="space-y-1.5">
                  <Label htmlFor="email" className="text-xs font-semibold uppercase tracking-wider text-[#56423c]">
                    Work Email *
                  </Label>
                  <Input
                    id="email"
                    type="email"
                    required
                    placeholder="alex@enterprise.com"
                    value={formData.email}
                    onChange={(e) => setFormData({ ...formData, email: e.target.value })}
                    className="bg-white/80 border-[#e6dfd8] focus-visible:ring-[#cc785c] rounded-xl text-sm h-10"
                  />
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div className="space-y-1.5">
                  <Label htmlFor="company" className="text-xs font-semibold uppercase tracking-wider text-[#56423c]">
                    Company Name *
                  </Label>
                  <Input
                    id="company"
                    required
                    placeholder="Meridian Capital"
                    value={formData.company}
                    onChange={(e) => setFormData({ ...formData, company: e.target.value })}
                    className="bg-white/80 border-[#e6dfd8] focus-visible:ring-[#cc785c] rounded-xl text-sm h-10"
                  />
                </div>

                <div className="space-y-1.5">
                  <Label htmlFor="industry" className="text-xs font-semibold uppercase tracking-wider text-[#56423c]">
                    Regulated Sector
                  </Label>
                  <select
                    id="industry"
                    value={formData.industry}
                    onChange={(e) => setFormData({ ...formData, industry: e.target.value })}
                    className="w-full bg-white/80 border border-[#e6dfd8] focus:border-[#cc785c] focus:outline-none focus:ring-1 focus:ring-[#cc785c] rounded-xl text-sm h-10 px-3 text-[#141413]"
                  >
                    <option value="Legal & Compliance">Legal & Compliance</option>
                    <option value="Financial Services & Banking">Financial Services & Banking</option>
                    <option value="Healthcare & Life Sciences">Healthcare & Life Sciences</option>
                    <option value="Government & Defense">Government & Defense</option>
                    <option value="Audit & Advisory">Audit & Advisory</option>
                  </select>
                </div>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="volume" className="text-xs font-semibold uppercase tracking-wider text-[#56423c]">
                  Estimated Document Volume
                </Label>
                <select
                  id="volume"
                  value={formData.docVolume}
                  onChange={(e) => setFormData({ ...formData, docVolume: e.target.value })}
                  className="w-full bg-white/80 border border-[#e6dfd8] focus:border-[#cc785c] focus:outline-none focus:ring-1 focus:ring-[#cc785c] rounded-xl text-sm h-10 px-3 text-[#141413]"
                >
                  <option value="< 10k pages/mo">&lt; 10,000 pages / month</option>
                  <option value="10k - 100k pages/mo">10,000 – 100,000 pages / month</option>
                  <option value="100k - 1M pages/mo">100,000 – 1,000,000 pages / month</option>
                  <option value="1M+ pages/mo">1,000,000+ pages / month (Enterprise Grid)</option>
                </select>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="note" className="text-xs font-semibold uppercase tracking-wider text-[#56423c]">
                  Specific Verification Requirements (Optional)
                </Label>
                <textarea
                  id="note"
                  rows={2}
                  placeholder="E.g. We need audit trails for SEC 10-K filings and complex multi-page financial tables..."
                  value={formData.note}
                  onChange={(e) => setFormData({ ...formData, note: e.target.value })}
                  className="w-full bg-white/80 border border-[#e6dfd8] focus:border-[#cc785c] focus:outline-none focus:ring-1 focus:ring-[#cc785c] rounded-xl text-sm p-3 text-[#141413] resize-none"
                />
              </div>

              <div className="pt-2 flex flex-col sm:flex-row items-center gap-3">
                <Button
                  type="submit"
                  disabled={isSubmitting}
                  className="w-full bg-[#cc785c] hover:bg-[#b56449] text-white font-medium rounded-xl h-11 text-sm shadow-xs transition-colors"
                >
                  {isSubmitting ? "Submitting Request..." : "Schedule Dedicated Demo"}
                </Button>
              </div>

              <div className="flex items-center justify-center gap-4 text-[11px] text-[#89726b] pt-1">
                <span className="flex items-center gap-1">
                  <ShieldCheck className="w-3.5 h-3.5 text-[#cc785c]" /> SOC2 Type II Certified
                </span>
                <span className="flex items-center gap-1">
                  <Building2 className="w-3.5 h-3.5 text-[#cc785c]" /> On-Prem / VPC Deployments
                </span>
              </div>
            </form>
          </div>
        ) : (
          <div className="py-6 text-center space-y-5">
            <div className="w-14 h-14 rounded-full bg-[#cc785c]/10 text-[#cc785c] flex items-center justify-center mx-auto ring-8 ring-[#cc785c]/5">
              <CheckCircle2 className="w-8 h-8" />
            </div>

            <div className="space-y-2">
              <h3 className="font-serif text-2xl sm:text-3xl text-[#141413]">
                Demo Request Confirmed
              </h3>
              <p className="text-sm text-[#56423c] max-w-sm mx-auto">
                Thank you, <span className="font-semibold text-[#141413]">{formData.name}</span>. A senior AI solutions engineer will reach out to <span className="font-semibold text-[#141413]">{formData.email}</span> within 2 business hours.
              </p>
            </div>

            <div className="p-4 bg-[#efe9de] border border-[#e6dfd8] rounded-xl text-left text-xs space-y-2 max-w-sm mx-auto">
              <div className="flex justify-between text-[#56423c]">
                <span>Target Entity:</span>
                <span className="font-semibold text-[#141413]">{formData.company}</span>
              </div>
              <div className="flex justify-between text-[#56423c]">
                <span>Domain Focus:</span>
                <span className="font-semibold text-[#141413]">{formData.industry}</span>
              </div>
              <div className="flex justify-between text-[#56423c]">
                <span>Expected SLA:</span>
                <span className="font-semibold text-[#cc785c]">Enterprise Guaranteed</span>
              </div>
            </div>

            <div className="pt-2 flex flex-col sm:flex-row gap-3 justify-center">
              <Button
                onClick={handleGoToWorkspace}
                className="bg-[#cc785c] hover:bg-[#b56449] text-white font-medium rounded-xl text-sm h-10 px-5 shadow-xs"
              >
                Launch Workspace Preview <ArrowRight className="w-4 h-4 ml-1.5" />
              </Button>
              <Button
                variant="outline"
                onClick={() => onOpenChange(false)}
                className="border-[#e6dfd8] text-[#56423c] hover:bg-[#efe9de] rounded-xl text-sm h-10"
              >
                Close
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
