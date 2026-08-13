import { useState, useEffect } from "react"
import { useNavigate } from "react-router-dom"
import {
  Plus,
  Search,
  Bell,
  DollarSign,
  Scale,
  Users2,
  Folder,
  FileText
} from "lucide-react"
import { useWorkspaceStore } from "../store/useWorkspaceStore"
import { WorkspaceCardSkeleton } from "../components/common/ShimmerSkeleton"
import { Input } from "../components/ui/input"
import { Dialog, DialogContent, DialogTitle } from "../components/ui/dialog"
import { CreateWorkspacePage } from "./CreateWorkspacePage"
import type { Workspace } from "../types/api"

export function WorkspacePage() {
  const navigate = useNavigate()
  const {
    workspaces,
    searchQuery,
    setSearchQuery,
    setActiveWorkspace,
    fetchWorkspaces,
    isLoading
  } = useWorkspaceStore()

  const [createModalOpen, setCreateModalOpen] = useState(false)

  useEffect(() => {
    fetchWorkspaces()
  }, [fetchWorkspaces])

  const filteredWorkspaces = workspaces.filter((ws) =>
    ws.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
    ws.description.toLowerCase().includes(searchQuery.toLowerCase())
  )

  const handleSelectWorkspace = (ws: Workspace) => {
    setActiveWorkspace(ws)
    // If workspace has no documents uploaded, redirect to the upload section
    if (ws.document_count === 0) {
      navigate(`/workspaces/${ws.id}/upload`)
    } else {
      navigate(`/workspaces/${ws.id}/chat`)
    }
  }

  const getWorkspaceIcon = (ws: Workspace) => {
    const name = ws.name.toLowerCase()
    if (name.includes("finance") || ws.icon_type === "finance") {
      return (
        <div className="h-9 w-9 rounded-lg bg-[#e2f3ee] dark:bg-[#1a3832] text-[#006768] dark:text-[#6cd7d8] flex items-center justify-center font-bold text-sm">
          <DollarSign className="h-4 w-4" />
        </div>
      )
    }
    if (name.includes("legal") || ws.icon_type === "legal") {
      return (
        <div className="h-9 w-9 rounded-lg bg-[#fdeae4] dark:bg-[#3d231b] text-[#c96442] dark:text-[#ffb59d] flex items-center justify-center font-bold text-sm">
          <Scale className="h-4 w-4" />
        </div>
      )
    }
    if (name.includes("engineering") || ws.icon_type === "engineering") {
      return (
        <div className="h-9 w-9 rounded-lg bg-[#ede9fe] dark:bg-[#2b2447] text-[#9c87f5] dark:text-[#c4b5fd] flex items-center justify-center font-bold text-sm">
          <Users2 className="h-4 w-4" />
        </div>
      )
    }
    return (
      <div className="h-9 w-9 rounded-lg bg-accent text-foreground flex items-center justify-center font-bold text-sm">
        <Folder className="h-4 w-4" />
      </div>
    )
  }

  return (
    <div className="p-8 lg:p-12 max-w-7xl mx-auto space-y-8 animate-in fade-in-50 duration-200">
      {/* Header Section */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 pb-2">
        <div className="space-y-1">
          <h1 className="font-headline text-4xl lg:text-5xl font-bold tracking-tight text-foreground">
            Workspaces
          </h1>
          <p className="text-sm text-muted-foreground font-sans">
            Manage your active recruiting pipelines and document sets.
          </p>
        </div>

        <div className="flex items-center gap-4">
          {/* Search bar */}
          <div className="relative w-72 sm:w-80">
            <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              type="text"
              placeholder="Search workspaces..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-10 pr-4 h-10 bg-card border-input/80 rounded-full text-xs placeholder:text-muted-foreground/70 focus-visible:ring-[#c96442]"
            />
          </div>

          {/* Notification bell */}
          <button
            type="button"
            className="relative p-2.5 rounded-full hover:bg-accent text-foreground transition-colors cursor-pointer"
            title="Notifications"
          >
            <Bell className="h-5 w-5" />
            <span className="absolute top-2 right-2 h-2 w-2 rounded-full bg-[#c96442]" />
          </button>
        </div>
      </div>

      {/* Grid of Workspaces */}
      {isLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          <WorkspaceCardSkeleton />
          <WorkspaceCardSkeleton />
          <WorkspaceCardSkeleton />
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {/* Card 1: Create New Workspace */}
          <button
            type="button"
            onClick={() => setCreateModalOpen(true)}
            className="h-64 rounded-2xl border-2 border-dashed border-border/80 hover:border-primary/80 bg-card/40 hover:bg-card/90 transition-all p-6 flex flex-col items-center justify-center text-center group cursor-pointer"
          >
            <div className="h-14 w-14 rounded-full bg-[#fdeae4] dark:bg-[#3d231b] group-hover:bg-primary text-[#c96442] dark:text-[#ffb59d] group-hover:text-white flex items-center justify-center transition-all shadow-xs mb-4">
              <Plus className="h-6 w-6 stroke-[2.5]" />
            </div>
            <h3 className="font-headline text-lg font-bold text-foreground group-hover:text-primary transition-colors">
              Create New Workspace
            </h3>
            <p className="text-xs text-muted-foreground mt-1.5 max-w-[200px] leading-relaxed">
              Start a new batch for candidate screening.
            </p>
          </button>

          {/* Existing Workspaces */}
          {filteredWorkspaces.map((ws) => (
            <div
              key={ws.id}
              onClick={() => handleSelectWorkspace(ws)}
              className="h-64 rounded-2xl border border-border/80 bg-card hover:border-primary/50 transition-all p-6 flex flex-col justify-between shadow-xs hover:shadow-md cursor-pointer group"
            >
              <div className="space-y-4">
                {/* Icon */}
                {getWorkspaceIcon(ws)}

                {/* Info */}
                <div className="space-y-1.5">
                  <h3 className="font-headline text-lg font-bold text-foreground group-hover:text-primary transition-colors">
                    {ws.name}
                  </h3>
                  <p className="text-xs text-muted-foreground line-clamp-2 leading-relaxed">
                    {ws.description}
                  </p>
                </div>
              </div>

              {/* Bottom stats */}
              <div className="pt-4 border-t border-border/50 flex items-center justify-between">
                <div className="flex items-center gap-1.5 text-xs text-muted-foreground font-medium">
                  <FileText className="h-3.5 w-3.5" />
                  <span>{ws.document_count} Docs</span>
                </div>
                <span className="px-2.5 py-1 rounded-full text-[11px] font-medium bg-[#ede9de] dark:bg-[#282a2c] text-foreground/80">
                  {ws.last_active}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Create Workspace Modal Dialog */}
      <Dialog open={createModalOpen} onOpenChange={setCreateModalOpen}>
        <DialogContent className="max-w-xl p-8 bg-card border border-border/90 rounded-3xl shadow-2xl">
          <DialogTitle className="sr-only">Create Workspace</DialogTitle>
          <CreateWorkspacePage onClose={() => setCreateModalOpen(false)} isModal={true} />
        </DialogContent>
      </Dialog>
    </div>
  )
}
