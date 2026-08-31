import { BrowserRouter, Routes, Route, Navigate, useParams } from "react-router-dom"
import { Toaster } from "sonner"
import { AuthPage } from "./pages/AuthPage"
import { WorkspacePage } from "./pages/WorkspacePage"
import { CreateWorkspacePage } from "./pages/CreateWorkspacePage"
import { LibraryPage } from "./pages/LibraryPage"
import { UploadPage } from "./pages/UploadPage"
import { DocumentViewerPage } from "./pages/DocumentViewerPage"
import { BenchmarksPage } from "./pages/BenchmarksPage"
import { ChatPage } from "./pages/ChatPage"
import { LandingPage } from "./pages/LandingPage"
import { MainLayout } from "./components/layout/MainLayout"
import { useWorkspaceStore } from "./store/useWorkspaceStore"

function DynamicWorkspaceRedirect({ target }: { target: "chat" | "documents" | "upload" }) {
  const { activeWorkspace, workspaces } = useWorkspaceStore()
  const wsId = activeWorkspace?.id || workspaces[0]?.id
  if (!wsId) {
    return <Navigate to="/workspaces" replace />
  }
  if (target === "documents") {
    return <Navigate to={`/library/${wsId}`} replace />
  }
  if (target === "upload") {
    return <Navigate to={`/library/${wsId}/upload`} replace />
  }
  return <Navigate to={`/workspaces/${wsId}/${target}`} replace />
}

function LegacyWorkspaceDocumentsRedirect() {
  const { workspaceId } = useParams<{ workspaceId: string }>()
  return <Navigate to={`/library/${workspaceId}`} replace />
}

function LegacyWorkspaceDocRedirect() {
  const { workspaceId, documentId } = useParams<{ workspaceId: string; documentId: string }>()
  return <Navigate to={`/library/${workspaceId}/document/${documentId}`} replace />
}

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* Enterprise Landing Page Route */}
        <Route path="/" element={<LandingPage />} />
        <Route path="/landing" element={<LandingPage />} />

        {/* Auth Route */}
        <Route path="/login" element={<AuthPage />} />

        {/* 3-Pane Chat Route (Independent 3-card layout) */}
        <Route path="/workspaces/:workspaceId/chat" element={<ChatPage />} />
        <Route path="/chat" element={<DynamicWorkspaceRedirect target="chat" />} />

        {/* Main 2-Panel App Layout */}
        <Route element={<MainLayout />}>
          <Route path="/workspaces" element={<WorkspacePage />} />
          <Route path="/workspaces/new" element={<CreateWorkspacePage />} />

          {/* Library & Document Viewer Routes */}
          <Route path="/library/:workspaceId" element={<LibraryPage />} />
          <Route path="/library/:workspaceId/upload" element={<UploadPage />} />
          <Route path="/library/:workspaceId/document/:documentId" element={<DocumentViewerPage />} />
          <Route path="/library/:workspaceId/doc/:documentId" element={<DocumentViewerPage />} />

          {/* Legacy & Shortcut redirects */}
          <Route path="/workspaces/:workspaceId/documents" element={<LegacyWorkspaceDocumentsRedirect />} />
          <Route path="/workspaces/:workspaceId/upload" element={<UploadPage />} />
          <Route path="/workspaces/:workspaceId/document/:documentId" element={<LegacyWorkspaceDocRedirect />} />
          <Route path="/workspaces/:workspaceId/doc/:documentId" element={<LegacyWorkspaceDocRedirect />} />
          <Route path="/library" element={<DynamicWorkspaceRedirect target="documents" />} />
          <Route path="/library/upload" element={<DynamicWorkspaceRedirect target="upload" />} />

          {/* Benchmarks & System */}
          <Route path="/benchmarks" element={<BenchmarksPage />} />

          {/* Placeholder Settings / Archived */}
          <Route
            path="/settings"
            element={
              <div className="p-8 space-y-4">
                <h1 className="font-headline text-3xl font-bold text-foreground">Settings</h1>
                <p className="text-sm text-muted-foreground">Workspace configuration & API tokens. xxx</p>
              </div>
            }
          />
          <Route
            path="/archived"
            element={
              <div className="p-8 space-y-4">
                <h1 className="font-headline text-3xl font-bold text-foreground">Archived Pipelines</h1>
                <p className="text-sm text-muted-foreground">Historical candidate retrieval indexes.</p>
              </div>
            }
          />
        </Route>

        {/* Catch-all redirect */}
        <Route path="*" element={<Navigate to="/workspaces" replace />} />
      </Routes>

      {/* Theme-Adaptive Toast Notifications with 2.8s optimal duration */}
      <Toaster
        position="top-right"
        duration={2800}
        closeButton
        toastOptions={{
          classNames: {
            toast:
              "group toast bg-card text-foreground border border-border/90 shadow-2xl rounded-2xl p-4 font-sans text-xs",
            description: "text-muted-foreground text-[11px]",
            actionButton: "bg-[#c96442] hover:bg-[#b05730] text-white rounded-xl font-medium",
            cancelButton: "bg-muted text-muted-foreground rounded-xl",
            closeButton: "bg-card border-border text-muted-foreground hover:text-foreground"
          }
        }}
      />
    </BrowserRouter>
  )
}
export default App
