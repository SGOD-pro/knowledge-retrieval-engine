import React, { useState, useRef, useEffect } from "react"
import { useParams } from "react-router-dom"
import {
  Send,
  Paperclip,
  Zap,
  Brain,
  Sparkles,
  FileSearch,
  PanelLeftOpen,
  Maximize,
  Minimize,
  HelpCircle
} from "lucide-react"
import { ThinkingOrb } from "thinking-orbs"
import { useChatStore } from "../store/useChatStore"
import { useWorkspaceStore } from "../store/useWorkspaceStore"
import { ChatSidebar } from "../components/chat/ChatSidebar"
import { DocViewerPane } from "../components/chat/DocViewerPane"
import { Button } from "../components/ui/button"
import { Dialog, DialogContent, DialogTitle } from "../components/ui/dialog"
import { UploadPage } from "./UploadPage"
import { ThemeToggle } from "../components/common/ThemeToggle"

export function ChatPage() {
  const { workspaceId } = useParams<{ workspaceId: string }>()
  const { activeWorkspace, workspaces, setActiveWorkspace } = useWorkspaceStore()
  const {
    sessions,
    activeSessionId,
    ensureSession,
    sendMessage,
    isQuerying,
    currentStage,
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

  const currentWsId = workspaceId || activeWorkspace?.id || ""

  // Sync route workspaceId with store & ensure session
  useEffect(() => {
    if (workspaceId) {
      const match = workspaces.find((w) => w.id === workspaceId)
      if (match && activeWorkspace?.id !== match.id) {
        setActiveWorkspace(match)
      }
    }
  }, [workspaceId, activeWorkspace?.id, workspaces, setActiveWorkspace])

  // Ensure an active session exists for this workspace
  useEffect(() => {
    if (currentWsId) {
      ensureSession(currentWsId)
    }
  }, [currentWsId, ensureSession])

  const activeSession = sessions.find((s) => s.id === activeSessionId) || sessions[0]

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [activeSession?.messages, isQuerying])

  // Auto-adaptive height for textarea
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto"
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 140)}px`
    }
  }, [inputQuery])

  const handleSend = async (e?: React.FormEvent, customText?: string) => {
    if (e) e.preventDefault()
    const textToSend = customText || inputQuery
    if (!textToSend.trim() || isQuerying) return
    setInputQuery("")
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto"
    }
    await sendMessage(currentWsId, textToSend)
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

  // Find last AI message to show metrics
  const lastAiMessage = activeSession?.messages
    ?.slice()
    .reverse()
    .find((m) => m.sender === "assistant" && m.latency_ms)

  const quickPrompts = [
    "Summarize the key findings and executive summary across uploaded documents.",
    "Extract all key entities, metrics, and dates mentioned in the documents.",
    "What are the main technical concepts or topics discussed in this corpus?"
  ]

  return (
    <div className="h-screen w-screen flex p-2 sm:p-2.5 gap-2 sm:gap-2.5 overflow-hidden bg-background text-foreground select-none">
      {/* Pane 1: Collapsible Chat Sidebar */}
      {leftPaneOpen && <ChatSidebar />}

      {/* Pane 2: Center Chat Area */}
      <div className="flex-1 flex flex-col h-full min-w-0 min-h-0 overflow-hidden bg-card border border-border/80 rounded-3xl shadow-xs">
        {/* Top Header (shrink-0) */}
        <div className="h-14 px-5 border-b border-border/60 flex items-center justify-between bg-card/40 shrink-0 z-10">
          <div className="flex items-center gap-3 min-w-0">
            {/* Reopen Left Sidebar Button if collapsed */}
            {!leftPaneOpen && (
              <button
                type="button"
                onClick={() => setLeftPaneOpen(true)}
                className="p-1.5 rounded-xl bg-card border border-border text-foreground hover:bg-accent text-xs font-semibold flex items-center gap-1.5 shadow-xs cursor-pointer shrink-0"
                title="Open Chat History"
              >
                <PanelLeftOpen className="h-4 w-4 text-[#c96442]" />
                <span className="hidden sm:inline text-xs">History</span>
              </button>
            )}

            <h1 className="font-headline font-bold text-base sm:text-lg text-foreground truncate">
              {activeSession?.title || "New Query Session"}
            </h1>
          </div>

          <div className="flex items-center gap-3 sm:gap-4 shrink-0">
            {/* Dynamic KPI metrics from real queries */}
            {lastAiMessage && (
              <div className="hidden sm:flex items-center gap-3 text-xs font-semibold">
                {lastAiMessage.latency_ms && (
                  <div className="flex items-center gap-1.5">
                    <span className="text-[10px] text-muted-foreground uppercase tracking-wider">
                      Latency
                    </span>
                    <span className="font-bold text-foreground">
                      {(lastAiMessage.latency_ms / 1000).toFixed(2)}s
                    </span>
                  </div>
                )}
                {lastAiMessage.faithfulness && (
                  <div className="flex items-center gap-1.5">
                    <span className="text-[10px] text-muted-foreground uppercase tracking-wider">
                      Faithfulness
                    </span>
                    <span className="font-bold text-[#006768] dark:text-[#6cd7d8]">
                      {lastAiMessage.faithfulness}%
                    </span>
                  </div>
                )}
              </div>
            )}

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
                <span>Document Pane</span>
              </button>
            )}
          </div>
        </div>

        {/* Scrollable Messages Area (flex-1 min-h-0) */}
        <div className="flex-1 min-h-0 overflow-y-auto p-4 sm:p-6 space-y-5">
          {(!activeSession?.messages || activeSession.messages.length === 0) ? (
            <div className="h-full min-h-[300px] flex flex-col items-center justify-center text-center p-4 max-w-lg mx-auto space-y-4 my-auto">
              <div className="h-12 w-12 rounded-2xl bg-[#fdeae4] dark:bg-[#3d231b] text-[#c96442] flex items-center justify-center shadow-xs">
                <Sparkles className="h-6 w-6" />
              </div>
              <div className="space-y-1.5">
                <h3 className="font-headline text-xl sm:text-2xl font-bold text-foreground">
                  Query {activeWorkspace?.name || "Workspace"}
                </h3>
                <p className="text-xs text-muted-foreground leading-relaxed max-w-sm">
                  Ask grounded questions across your workspace corpus. KRE will retrieve matching chunks and provide verifiable citations.
                </p>
              </div>

              {/* Suggested starter questions */}
              <div className="w-full space-y-2 pt-2 text-left">
                <div className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground px-1 flex items-center gap-1">
                  <HelpCircle className="h-3 w-3 text-[#c96442]" />
                  <span>Suggested queries</span>
                </div>
                <div className="space-y-1.5">
                  {quickPrompts.map((prompt, idx) => (
                    <button
                      key={idx}
                      type="button"
                      onClick={() => handleSend(undefined, prompt)}
                      className="w-full text-left p-2.5 rounded-xl border border-border/80 hover:border-[#c96442]/60 bg-muted/30 hover:bg-[#c96442]/5 text-xs text-foreground/90 hover:text-foreground transition-all cursor-pointer"
                    >
                      {prompt}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            activeSession.messages.map((msg) => (
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
                  <div className="max-w-2xl space-y-2.5 w-full">
                    {/* Bot Label */}
                    <div className="flex items-center gap-2">
                      <div className="h-5 w-5 rounded-md bg-[#c96442] text-white flex items-center justify-center text-[9px] font-bold shadow-xs">
                        KRE
                      </div>
                      <span className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
                        KRE AI
                      </span>
                      {msg.timestamp && (
                        <span className="text-[10px] text-muted-foreground">
                          {msg.timestamp}
                        </span>
                      )}
                    </div>

                    {/* AI Response Text */}
                    <div className="text-foreground text-sm font-sans leading-relaxed whitespace-pre-line bg-muted/20 p-4 rounded-2xl border border-border/60 shadow-xs">
                      {renderMessageContent(msg.text, msg.citations)}
                    </div>

                    {/* Retrieval Tag Chips */}
                    <div className="flex items-center gap-2 pt-0.5">
                      {msg.retrieval_path && (
                        <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-[11px] font-medium bg-[#ede9de] dark:bg-[#242628] text-foreground/80">
                          <Zap className="h-3 w-3 text-[#c96442]" />
                          <span>{msg.retrieval_path}</span>
                        </span>
                      )}
                      {msg.confidence !== undefined && (
                        <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-[11px] font-medium bg-[#ede9de] dark:bg-[#242628] text-foreground/80">
                          <Brain className="h-3 w-3 text-[#006768] dark:text-[#6cd7d8]" />
                          <span>Confidence {Math.round(msg.confidence * 100)}%</span>
                        </span>
                      )}
                    </div>
                  </div>
                )}
              </div>
            ))
          )}

          {/* Real-time Streaming Processing Card with ThinkingOrb */}
          {isQuerying && (
            <div className="flex items-start gap-4 p-4 rounded-3xl bg-[#ede9de]/80 dark:bg-[#242628]/80 border border-border/80 shadow-xs max-w-xl animate-in fade-in-50 duration-200">
              <div className="shrink-0 pt-0.5">
                <ThinkingOrb
                  state={currentStage?.state || "listening"}
                  size={64}
                />
              </div>
              <div className="space-y-1.5 min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
                    Retrieval Engine
                  </span>
                  {currentStage?.path && (
                    <span
                      className={`inline-flex items-center gap-1 px-2 py-0.5 text-[10px] font-bold rounded-full ${
                        currentStage.path === "fast"
                          ? "bg-[#c96442]/15 text-[#c96442]"
                          : "bg-[#006768]/15 text-[#006768] dark:text-[#6cd7d8]"
                      }`}
                    >
                      {currentStage.path === "fast" ? (
                        <>
                          <Zap className="h-2.5 w-2.5" />
                          <span>⚡ Fast Match Path</span>
                        </>
                      ) : (
                        <>
                          <Brain className="h-2.5 w-2.5" />
                          <span>🧠 Deep Multi-Hop Path</span>
                        </>
                      )}
                    </span>
                  )}
                </div>
                <p className="text-xs font-semibold text-foreground leading-snug animate-pulse">
                  {currentStage?.label || "Analyzing query & planning route..."}
                </p>
                {/* Dynamic stage step tracker */}
                <div className="flex items-center gap-1.5 pt-1 text-[10px] text-muted-foreground font-mono">
                  <span
                    className={`px-1.5 py-0.5 rounded-md ${
                      currentStage?.stage === "listening" || currentStage?.stage === "routing"
                        ? "bg-[#c96442]/20 text-[#c96442] font-bold"
                        : "opacity-60"
                    }`}
                  >
                    1. Plan
                  </span>
                  <span>→</span>
                  <span
                    className={`px-1.5 py-0.5 rounded-md ${
                      currentStage?.stage === "searching"
                        ? "bg-[#c96442]/20 text-[#c96442] font-bold"
                        : "opacity-60"
                    }`}
                  >
                    2. Vector
                  </span>
                  {currentStage?.path !== "fast" && (
                    <>
                      <span>→</span>
                      <span
                        className={`px-1.5 py-0.5 rounded-md ${
                          currentStage?.stage === "graph"
                            ? "bg-[#c96442]/20 text-[#c96442] font-bold"
                            : "opacity-60"
                        }`}
                      >
                        3. Graph
                      </span>
                    </>
                  )}
                  <span>→</span>
                  <span
                    className={`px-1.5 py-0.5 rounded-md ${
                      currentStage?.stage === "synthesis"
                        ? "bg-[#c96442]/20 text-[#c96442] font-bold"
                        : "opacity-60"
                    }`}
                  >
                    {currentStage?.path === "fast" ? "3. Match" : "4. Synthesize"}
                  </span>
                </div>
              </div>
            </div>
          )}

          <div ref={messagesEndRef} className="h-2" />
        </div>

        {/* Dedicated Chat Input Area (shrink-0, non-overlapping flex sibling) */}
        <div className="shrink-0 p-3 sm:p-4 pt-2 border-t border-border/50 bg-card/90 backdrop-blur-sm flex flex-col items-center">
          <div className="w-full max-w-3xl flex flex-col items-center space-y-1.5">
            <form
              onSubmit={(e) => handleSend(e)}
              className="w-full relative flex items-end bg-[#ede9de] dark:bg-[#242628] rounded-3xl border border-[#ded8cd] dark:border-[#333537] shadow-sm px-2 py-1.5 transition-all focus-within:border-[#c96442] dark:focus-within:border-[#c96442]"
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
                placeholder="Ask a question across workspace documents..."
                disabled={isQuerying}
                className="flex-1 bg-transparent border-0 outline-none focus:outline-none focus:ring-0 focus-visible:ring-0 focus-visible:outline-none shadow-none text-sm placeholder:text-muted-foreground/70 resize-none py-2 px-2.5 max-h-36 overflow-y-auto leading-relaxed text-foreground font-sans scrollbar-thin"
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
