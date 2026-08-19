import { useState } from "react"
import { useNavigate } from "react-router-dom"
import {
  Plus,
  Search,
  MessageSquare,
  Folder,
  ArrowLeft,
  PanelLeftClose
} from "lucide-react"
import { useChatStore } from "../../store/useChatStore"
import { useWorkspaceStore } from "../../store/useWorkspaceStore"
import { useAuthStore } from "../../store/useAuthStore"
import { Button } from "../ui/button"
import { Avatar, AvatarFallback, AvatarImage } from "../ui/avatar"
import { Input } from "../ui/input"
import { ThemeToggle } from "../common/ThemeToggle"

export function ChatSidebar() {
  const navigate = useNavigate()
  const { activeWorkspace } = useWorkspaceStore()
  const { user } = useAuthStore()
  const {
    sessions,
    activeSessionId,
    setActiveSessionId,
    createNewChat,
    searchFilter,
    setSearchFilter,
    setLeftPaneOpen
  } = useChatStore()

  const [isSearching, setIsSearching] = useState(false)

  const currentWsId = activeWorkspace?.id || ""

  const handleNewChat = () => {
    const newId = createNewChat(currentWsId)
    setActiveSessionId(newId)
  }

  const workspaceSessions = sessions.filter(
    (s) => !currentWsId || s.workspaceId === currentWsId
  )

  const filteredSessions = workspaceSessions.filter((s) =>
    s.title.toLowerCase().includes(searchFilter.toLowerCase())
  )

  const todaySessions = filteredSessions.filter((s) => s.category === "Today")
  const previousSessions = filteredSessions.filter((s) => s.category !== "Today")

  const userInitials = (user?.name || "KRE")
    .split(" ")
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase()

  return (
    <aside className="w-64 sm:w-72 shrink-0 bg-sidebar border border-border/80 rounded-3xl p-4 flex flex-col justify-between h-full select-none shadow-xs animate-in slide-in-from-left-2 duration-150">
      {/* Top Section */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Workspace Title, Back button & Collapse button */}
        <div className="flex items-start justify-between mb-4">
          <div className="space-y-0.5 min-w-0 pr-1">
            <h2 className="font-headline font-bold text-base sm:text-lg text-foreground truncate">
              {activeWorkspace?.name || "Workspace"}
            </h2>
            <p className="text-[11px] text-muted-foreground font-sans">
              {activeWorkspace?.document_count
                ? `${activeWorkspace.document_count} Document${activeWorkspace.document_count > 1 ? "s" : ""}`
                : "Active Corpus"}
            </p>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            <button
              type="button"
              onClick={() => navigate("/workspaces")}
              className="p-1 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-colors cursor-pointer"
              title="Back to Workspaces"
            >
              <ArrowLeft className="h-4 w-4" />
            </button>
            <button
              type="button"
              onClick={() => setLeftPaneOpen(false)}
              className="p-1 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent transition-colors cursor-pointer"
              title="Collapse Sidebar"
            >
              <PanelLeftClose className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* Action Buttons: + New Chat & Search */}
        <div className="flex items-center gap-2 mb-4">
          <Button
            type="button"
            onClick={handleNewChat}
            variant="outline"
            className="flex-1 h-9 border-[#c96442]/60 hover:border-[#c96442] hover:bg-[#c96442]/10 text-foreground font-semibold rounded-xl text-xs flex items-center justify-center gap-1.5 shadow-xs transition-colors cursor-pointer"
          >
            <Plus className="h-3.5 w-3.5 text-[#c96442]" />
            <span>New Chat</span>
          </Button>

          <Button
            type="button"
            onClick={() => setIsSearching(!isSearching)}
            className="h-9 w-9 bg-[#c96442] hover:bg-[#b05730] text-white rounded-xl flex items-center justify-center shadow-xs cursor-pointer p-0"
            title="Search Chats"
          >
            <Search className="h-3.5 w-3.5" />
          </Button>
        </div>

        {/* Search Input if toggled */}
        {isSearching && (
          <div className="mb-3 animate-in fade-in-50 duration-150">
            <Input
              type="text"
              placeholder="Filter chats..."
              value={searchFilter}
              onChange={(e) => setSearchFilter(e.target.value)}
              className="h-8 text-xs bg-card border-input rounded-xl"
              autoFocus
            />
          </div>
        )}

        {/* Chat History Lists */}
        <div className="flex-1 overflow-y-auto space-y-5 pr-1">
          {/* Today */}
          {todaySessions.length > 0 && (
            <div className="space-y-1.5">
              <span className="text-[10px] font-bold text-muted-foreground uppercase tracking-wider px-2">
                Today
              </span>
              <div className="space-y-1">
                {todaySessions.map((session) => {
                  const isActive = session.id === activeSessionId
                  return (
                    <button
                      key={session.id}
                      type="button"
                      onClick={() => setActiveSessionId(session.id)}
                      className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-xl text-xs text-left transition-all cursor-pointer ${
                        isActive
                          ? "bg-[#ede9de] dark:bg-[#282a2c] text-[#c96442] dark:text-[#ffb59d] font-bold shadow-xs"
                          : "text-sidebar-foreground/85 hover:bg-sidebar-accent/60 font-medium"
                      }`}
                    >
                      <MessageSquare className="h-3.5 w-3.5 shrink-0" />
                      <span className="truncate">{session.title}</span>
                    </button>
                  )
                })}
              </div>
            </div>
          )}

          {/* Previous */}
          {previousSessions.length > 0 && (
            <div className="space-y-1.5">
              <span className="text-[10px] font-bold text-muted-foreground uppercase tracking-wider px-2">
                Previous Chats
              </span>
              <div className="space-y-1">
                {previousSessions.map((session) => {
                  const isActive = session.id === activeSessionId
                  return (
                    <button
                      key={session.id}
                      type="button"
                      onClick={() => setActiveSessionId(session.id)}
                      className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-xl text-xs text-left transition-all cursor-pointer ${
                        isActive
                          ? "bg-[#ede9de] dark:bg-[#282a2c] text-[#c96442] dark:text-[#ffb59d] font-bold shadow-xs"
                          : "text-sidebar-foreground/85 hover:bg-sidebar-accent/60 font-medium"
                      }`}
                    >
                      <Folder className="h-3.5 w-3.5 shrink-0 opacity-70" />
                      <span className="truncate">{session.title}</span>
                    </button>
                  )
                })}
              </div>
            </div>
          )}

          {filteredSessions.length === 0 && (
            <div className="text-center p-4 text-xs text-muted-foreground font-sans">
              No conversations yet.
            </div>
          )}
        </div>
      </div>

      {/* Bottom Section with New Workspace, User Profile and Theme Toggle */}
      <div className="pt-3 border-t border-border/60 space-y-3">
        <Button
          type="button"
          variant="outline"
          onClick={() => navigate("/workspaces")}
          className="w-full h-9 border-[#c96442]/60 hover:border-[#c96442] hover:bg-[#c96442]/10 text-foreground font-semibold rounded-xl text-xs flex items-center justify-center gap-1.5 shadow-xs cursor-pointer"
        >
          <Plus className="h-3.5 w-3.5 text-[#c96442]" />
          <span>New Workspace</span>
        </Button>

        {/* User Card & Theme Toggle */}
        <div className="flex items-center justify-between pt-0.5">
          <div className="flex items-center gap-2.5 min-w-0">
            <Avatar className="h-8 w-8 border border-border/70 shrink-0">
              <AvatarImage
                src={user?.avatar || undefined}
                alt={user?.name || "User"}
              />
              <AvatarFallback className="bg-primary/10 text-primary font-medium text-xs">
                {userInitials}
              </AvatarFallback>
            </Avatar>
            <div className="space-y-0.5 leading-none min-w-0">
              <div className="font-semibold text-xs text-foreground truncate">
                {user?.name || "KRE Analyst"}
              </div>
              <div className="text-[9px] font-bold text-muted-foreground uppercase tracking-wider truncate">
                {user?.role || "Analyst"}
              </div>
            </div>
          </div>

          {/* Theme Toggle Button */}
          <ThemeToggle />
        </div>
      </div>
    </aside>
  )
}
