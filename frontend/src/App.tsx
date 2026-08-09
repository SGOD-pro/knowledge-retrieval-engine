import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom"
import { Shell } from "./components/layout/Shell"
import { Library } from "./pages/Library"
import { Workspace } from "./pages/Workspace"
import { Benchmarks } from "./pages/Benchmarks"
import { KnowledgeGraph } from "./pages/KnowledgeGraph"

export default function App() {
  
  return (
    <BrowserRouter>
      <Shell>
        <Routes>
          <Route path="/" element={<Navigate to="/workspace" replace />} />
          <Route path="/workspace" element={<Workspace />} />
          <Route path="/library" element={<Library />} />
          <Route path="/graph" element={<KnowledgeGraph />} />
          <Route path="/benchmarks" element={<Benchmarks />} />
        </Routes>
      </Shell>
    </BrowserRouter>
  )
}
