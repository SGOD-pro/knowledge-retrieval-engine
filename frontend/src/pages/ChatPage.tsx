import React, { useState, useRef, useEffect } from "react"
import { useParams, useNavigate } from "react-router-dom"
import {
  Send,
  Paperclip,
  Zap,
  Brain,
  Sparkles,
  Loader2,
  FileSearch,
  PanelLeftOpen,
  Maximize,
  Minimize
} from "lucide-react"
import { useChatStore } from "../store/useChatStore"
import { useWorkspaceStore } from "../store/useWorkspaceStore"
import { ChatSidebar } from "../components/chat/ChatSidebar"
import { DocViewerPane } from "../components/chat/DocViewerPane"
import { Button } from "../components/ui/button"
import { Dialog, DialogContent, DialogTitle } from "../components/ui/dialog"
import { UploadPage } from "./UploadPage"
import { ThemeToggle } from "../components/common/ThemeToggle"

export function ChatPage() {
  const navigate = useNavigate()
  const { workspaceId } = useParams<{ workspaceId: string }>()
  const { activeWorkspace, workspaces, setActiveWorkspace } = useWorkspaceStore()
  const {
    sessions,
    activeSessionId,
    sendMessage,
    isQuerying,
    setActiveCitation,
    rightPaneOpen,
    setRightPaneOpen,
    leftPaneOpen,
    setLeftPaneOpen
  } = useChatStore()

  const [inputQuery, setInputQuery] = useState("")
  const [uploadModalOpen, setUploadModalOpen] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // Sync route workspaceId with store & redirect to upload if 0 docs
  useEffect(() => {
    if (workspaceId) {
      const match = workspaces.find((w) => w.id === workspaceId)
      if (match) {
        if (activeWorkspace?.id !== match.id) {
          setActiveWorkspace(match)
        }
        if (match.document_count === 0) {
          navigate(`/workspaces/${match.id}/upload`, { replace: true })
        }
      }
    } else if (activeWorkspace && activeWorkspace.document_count === 0) {
      navigate(`/workspaces/${activeWorkspace.id}/upload`, { replace: true })
    }
  }, [workspaceId, activeWorkspace, workspaces, setActiveWorkspace, navigate])

  const activeSession =
    sessions.find((s) => s.id === activeSessionId) || sessions[0]

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [activeSession?.messages, isQuerying])

  // Auto-adaptive height for textarea
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto"
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 160)}px`
    }
  }, [inputQuery])

  const handleSend = async (e?: React.FormEvent) => {
    if (e) e.preventDefault()
    if (!inputQuery.trim() || isQuerying) return
    const q = inputQuery
    setInputQuery("")
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto"
    }
    await sendMessage(workspaceId || activeWorkspace?.id || "ws_001", q)
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  // Toggle distraction-free full screen chat
  const isFullScreenChat = !leftPaneOpen && !rightPaneOpen
  const toggleFullScreenChat = () => {
    if (isFullScreenChat) {
      setLeftPaneOpen(true)
      setRightPaneOpen(true)
    } else {
      setLeftPaneOpen(false)
      setRightPaneOpen(false)
    }
  }

  // Parse text to turn [1], [2], etc. into interactive citation badges
  const renderMessageContent = (text: string, citations?: any[]) => {
    const parts = text.split(/(\[\d+\])/g)
    return parts.map((part, index) => {
      const match = part.match(/\[(\d+)\]/)
      if (match && citations) {
        const citationId = parseInt(match[1], 10)
        const citationObj = citations.find((c) => c.id === citationId)
        return (
          <button
            key={index}
            type="button"
            onClick={() => {
              if (citationObj) {
                setActiveCitation(citationObj)
              }
            }}
            className="inline-flex items-center justify-center h-4 w-4 rounded-full bg-[#c96442] text-white text-[10px] font-bold mx-1 hover:scale-110 hover:bg-[#b05730] transition-all cursor-pointer shadow-xs align-baseline"
            title={`Jump to citation [${citationId}] in document viewer`}
          >
            {citationId}
          </button>
        )
      }
      return <span key={index}>{part}</span>
    })
  }

  return (
    <div className="h-screen w-full flex p-2 sm:p-2.5 gap-2 sm:gap-2.5 overflow-hidden bg-background text-foreground select-none">
      {/* Pane 1: Collapsible Chat Sidebar */}
      {leftPaneOpen && <ChatSidebar />}

      {/* Pane 2: Center Chat Area */}
      <div className="flex-1 flex flex-col h-full overflow-hidden bg-card border border-border/80 rounded-3xl shadow-xs relative transition-all">
        {/* Top Header */}
        <div className="h-14 px-5 border-b border-border/60 flex items-center justify-between bg-card/40 shrink-0 z-10">
          <div className="flex items-center gap-3">
            {/* Reopen Left Sidebar Button if collapsed */}
            {!leftPaneOpen && (
              <button
                type="button"
                onClick={() => setLeftPaneOpen(true)}
                className="p-1.5 rounded-xl bg-card border border-border text-foreground hover:bg-accent text-xs font-semibold flex items-center gap-1.5 shadow-xs cursor-pointer"
                title="Open Chat History"
              >
                <PanelLeftOpen className="h-4 w-4 text-[#c96442]" />
                <span className="hidden sm:inline text-xs">History</span>
              </button>
            )}

            <h1 className="font-headline font-bold text-lg text-foreground truncate max-w-[280px] sm:max-w-md">
              {activeSession?.title || "Data Scientist Pipeline"}
            </h1>
          </div>

          <div className="flex items-center gap-3 sm:gap-4">
            {/* KPI metrics */}
            <div className="hidden sm:flex items-center gap-3 text-xs font-semibold">
              <div className="flex items-center gap-1.5">
                <span className="text-[10px] text-muted-foreground uppercase tracking-wider">
                  Latency
                </span>
                <span className="font-bold text-foreground">1.2s</span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="text-[10px] text-muted-foreground uppercase tracking-wider">
                  Faithfulness
                </span>
                <span className="font-bold text-[#006768] dark:text-[#6cd7d8]">
                  98%
                </span>
              </div>
            </div>

            {/* Theme Toggle in Header if left sidebar is collapsed */}
            {!leftPaneOpen && <ThemeToggle />}

            {/* Toggle Fullscreen Focus Mode */}
            <button
              type="button"
              onClick={toggleFullScreenChat}
              className="p-1.5 rounded-xl bg-card border border-border text-muted-foreground hover:text-foreground hover:bg-accent text-xs transition-colors cursor-pointer"
              title={isFullScreenChat ? "Exit Fullscreen Chat" : "Fullscreen Chat Mode"}
            >
              {isFullScreenChat ? (
                <Minimize className="h-4 w-4" />
              ) : (
                <Maximize className="h-4 w-4" />
              )}
            </button>

            {/* Toggle 3rd pane if closed */}
            {!rightPaneOpen && (
              <button
                type="button"
                onClick={() => setRightPaneOpen(true)}
                className="p-1.5 px-3 rounded-xl bg-card border border-border text-foreground hover:bg-accent text-xs font-semibold flex items-center gap-1.5 shadow-xs cursor-pointer"
              >
                <FileSearch className="h-4 w-4 text-[#c96442]" />
                <span>Open Document</span>
              </button>
            )}
          </div>
        </div>

        {/* Messages Scroll Area with Bottom Padding for Floating Box */}
        <div className="flex-1 overflow-y-auto p-5 sm:p-7 pb-36 space-y-5">
          {activeSession?.messages.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-center p-8 max-w-md mx-auto space-y-3">
              <div className="h-12 w-12 rounded-2xl bg-[#fdeae4] dark:bg-[#3d231b] text-[#c96442] flex items-center justify-center shadow-xs">
                <Sparkles className="h-6 w-6" />
              </div>
              <h3 className="font-headline text-2xl font-bold text-foreground">
                Query {activeWorkspace?.name || "Workspace"}
              </h3>
              <p className="text-xs text-muted-foreground leading-relaxed">
                Ask analytical questions across candidate resumes, legal portfolios, or technical docs with verifiable citations.
              </p>
            </div>
          ) : (
            activeSession?.messages.map((msg) => (
              <div
                key={msg.id}
                className={`flex flex-col ${
                  msg.sender === "user" ? "items-end" : "items-start"
                }`}
              >
                {/* User Message */}
                {msg.sender === "user" ? (
                  <div className="max-w-xl bg-[#ede9de] dark:bg-[#242628] text-foreground p-3.5 sm:p-4 rounded-2xl shadow-xs text-sm font-sans leading-relaxed">
                    {msg.text}
                  </div>
                ) : (
                  /* Assistant Message */
                  <div className="max-w-2xl space-y-2.5">
                    {/* Bot Label */}
                    <div className="flex items-center gap-2">
                      <div className="h-5 w-5 rounded-md bg-[#c96442] text-white flex items-center justify-center text-[9px] font-bold shadow-xs">
                        KRE
                      </div>
                      <span className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
                        KRE AI
                      </span>
                    </div>

                    {/* AI Response Text */}
                    <div className="text-foreground text-sm font-sans leading-relaxed whitespace-pre-line">
                      {renderMessageContent(msg.text, msg.citations)}
                    </div>

                    {/* Retrieval Tag Chips */}
                    <div className="flex items-center gap-2 pt-1.5">
                      <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-[11px] font-medium bg-[#ede9de] dark:bg-[#242628] text-foreground/80">
                        <Zap className="h-3 w-3 text-[#c96442]" />
                        <span>{msg.retrieval_path || "Fast Match (1.2s)"}</span>
                      </span>
                      <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-[11px] font-medium bg-[#ede9de] dark:bg-[#242628] text-foreground/80">
                        <Brain className="h-3 w-3 text-[#006768] dark:text-[#6cd7d8]" />
                        <span>Reasoned Answer</span>
                      </span>
                    </div>
                  </div>
                )}
              </div>
            ))
          )}

          {/* Typing / Querying indicator */}
          {isQuerying && (
            <div className="flex items-center gap-2 text-muted-foreground text-xs p-2">
              <Loader2 className="h-4 w-4 animate-spin text-[#c96442]" />
              <span>Retrieving candidates & synthesising citations...</span>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Smooth Bottom-to-Top Card Background Gradient Overlay */}
        <div className="absolute bottom-0 left-0 right-0 h-32 bg-gradient-to-t from-card via-card/85 to-transparent pointer-events-none z-10" />

        {/* Floating Chat Input Capsule with Auto-Adaptive Height & Zero Inner Outline */}
        <div className="absolute bottom-3 left-4 right-4 sm:left-8 sm:right-8 flex flex-col items-center pointer-events-none z-20">
          <div className="w-full max-w-3xl pointer-events-auto flex flex-col items-center space-y-1.5">
            <form
              onSubmit={handleSend}
              className="w-full relative flex items-end bg-[#ede9de] dark:bg-[#242628] rounded-3xl border border-[#ded8cd] dark:border-[#333537] shadow-lg shadow-black/5 dark:shadow-black/25 px-2 py-1.5 transition-all focus-within:border-[#c96442] dark:focus-within:border-[#c96442]"
            >
              <button
                type="button"
                onClick={() => setUploadModalOpen(true)}
                className="p-2 ml-1 rounded-full text-muted-foreground hover:text-foreground hover:bg-black/5 dark:hover:bg-white/5 transition-colors cursor-pointer shrink-0 mb-0.5"
                title="Attach Document to Workspace"
              >
                <Paperclip className="h-4 w-4" />
              </button>

              <textarea
                ref={textareaRef}
                rows={1}
                value={inputQuery}
                onChange={(e) => setInputQuery(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Ask about this candidate..."
                disabled={isQuerying}
                className="flex-1 bg-transparent border-0 outline-none focus:outline-none focus:ring-0 focus-visible:ring-0 focus-visible:outline-none shadow-none text-sm placeholder:text-muted-foreground/70 resize-none py-2 px-2.5 max-h-40 overflow-y-auto leading-relaxed text-foreground font-sans scrollbar-thin"
              />

              <Button
                type="submit"
                disabled={!inputQuery.trim() || isQuerying}
                className="h-9 w-11 bg-[#c96442] hover:bg-[#b05730] text-white rounded-xl shadow-xs shrink-0 flex items-center justify-center cursor-pointer transition-colors mr-1 mb-0.5"
              >
                <Send className="h-4 w-4" />
              </Button>
            </form>

            <p className="text-[10px] text-muted-foreground/75 text-center font-sans tracking-wide">
              AI can make mistakes. Verify critical claims.
            </p>
          </div>
        </div>
      </div>

      {/* Pane 3: Resizable & Collapsible Document Viewer / OKF Graph */}
      {rightPaneOpen && <DocViewerPane />}

      {/* Upload Documents Modal Dialog triggered via Paperclip Attachment */}
      <Dialog open={uploadModalOpen} onOpenChange={setUploadModalOpen}>
        <DialogContent className="max-w-2xl p-6 sm:p-8 bg-card border border-border/90 rounded-3xl shadow-2xl">
          <DialogTitle className="sr-only">Upload Documents</DialogTitle>
          <UploadPage isModal={true} onClose={() => setUploadModalOpen(false)} />
        </DialogContent>
      </Dialog>
    </div>
  )
}
