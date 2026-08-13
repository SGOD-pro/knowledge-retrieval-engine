import React, { useState } from "react"
import { useNavigate } from "react-router-dom"
import { X, Loader2 } from "lucide-react"
import { useWorkspaceStore } from "../store/useWorkspaceStore"
import { Input } from "../components/ui/input"
import { Textarea } from "../components/ui/textarea"
import { Button } from "../components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue
} from "../components/ui/select"
import { toast } from "sonner"

export function CreateWorkspacePage({
  onClose,
  isModal = false
}: {
  onClose?: () => void
  isModal?: boolean
}) {
  const navigate = useNavigate()
  const { createWorkspace, setActiveWorkspace, isLoading } = useWorkspaceStore()
  const [name, setName] = useState("")
  const [industry, setIndustry] = useState("")
  const [description, setDescription] = useState("")

  const handleCancel = () => {
    if (onClose) {
      onClose()
    } else {
      navigate("/workspaces")
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) {
      toast.error("Please enter a workspace name")
      return
    }

    const newWs = await createWorkspace({
      name,
      industry: industry || "General",
      description: description || "Knowledge collection for analytical retrieval."
    })

    if (newWs) {
      setActiveWorkspace(newWs)
      toast.success(`Workspace "${newWs.name}" created successfully!`)
      if (onClose) {
        onClose()
      }
      navigate(`/workspaces/${newWs.id}/upload`)
    } else {
      toast.error("Failed to create workspace")
    }
  }

  return (
    <div
      className={
        isModal
          ? "w-full space-y-6 animate-in fade-in-50 duration-150"
          : "min-h-screen bg-background flex flex-col items-center p-6 relative animate-in fade-in-50 duration-200"
      }
    >
      {/* Top Header only if NOT in modal */}
      {!isModal && (
        <div className="w-full max-w-4xl flex items-center justify-between py-2 mb-8">
          <div className="flex items-center gap-2">
            <div className="h-9 w-9 rounded-lg bg-card border border-border flex items-center justify-center p-1.5 shadow-xs">
              <img src="/logo.png" alt="KRE" className="h-full w-full object-contain" />
            </div>
          </div>
          <button
            type="button"
            onClick={handleCancel}
            className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors cursor-pointer"
          >
            <X className="h-4 w-4" />
            <span>Cancel</span>
          </button>
        </div>
      )}

      {/* Main Creation Card */}
      <div
        className={
          isModal
            ? "w-full space-y-6"
            : "w-full max-w-2xl bg-card rounded-3xl border border-border/80 p-8 sm:p-10 shadow-xs"
        }
      >
        <div className="space-y-1">
          <h1 className="font-headline text-2xl sm:text-3xl font-bold text-foreground">
            Create Workspace
          </h1>
          <p className="text-xs sm:text-sm text-muted-foreground font-sans">
            Configure a new environment for knowledge retrieval and analysis.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-5">
          {/* Workspace Name */}
          <div className="space-y-2">
            <label className="text-xs font-semibold text-foreground/90">
              Workspace Name
            </label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g., Engineering R&D"
              className="h-11 bg-card border-input/80 rounded-xl text-sm placeholder:text-muted-foreground/60"
              required
            />
          </div>

          {/* Industry */}
          <div className="space-y-2">
            <label className="text-xs font-semibold text-foreground/90">
              Industry
            </label>
            <Select value={industry} onValueChange={setIndustry}>
              <SelectTrigger className="h-11 bg-card border-input/80 rounded-xl text-sm">
                <SelectValue placeholder="Select an industry..." />
              </SelectTrigger>
              <SelectContent className="bg-popover border-border">
                <SelectItem value="Technology">Technology & Engineering</SelectItem>
                <SelectItem value="Finance">Finance & Investment</SelectItem>
                <SelectItem value="Legal">Legal & Compliance</SelectItem>
                <SelectItem value="Healthcare">Healthcare & Life Sciences</SelectItem>
                <SelectItem value="Consulting">Strategy & Consulting</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {/* Description */}
          <div className="space-y-2">
            <label className="text-xs font-semibold text-foreground/90">
              Initial Document Set Description
            </label>
            <Textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Briefly describe the types of documents you will be analyzing (e.g., Q3 financial reports, clinical trial data)..."
              rows={4}
              className="bg-card border-input/80 rounded-xl text-sm placeholder:text-muted-foreground/60 resize-y"
            />
          </div>

          {/* Actions */}
          <div className="pt-3 flex items-center justify-end gap-3">
            <Button
              type="button"
              variant="ghost"
              onClick={handleCancel}
              className="text-xs font-medium text-muted-foreground hover:text-foreground rounded-xl cursor-pointer"
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={isLoading}
              className="h-10 px-6 bg-[#c96442] hover:bg-[#b05730] text-white font-medium rounded-xl text-xs shadow-xs cursor-pointer"
            >
              {isLoading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                "Create Workspace"
              )}
            </Button>
          </div>
        </form>
      </div>
    </div>
  )
}
