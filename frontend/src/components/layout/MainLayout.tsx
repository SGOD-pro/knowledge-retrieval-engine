import { Outlet } from "react-router-dom"
import { AppSidebar } from "./AppSidebar"

export function MainLayout() {
  return (
    <div className="min-h-screen bg-background text-foreground p-3 sm:p-4 md:p-5 flex gap-4 md:gap-5 overflow-hidden">
      {/* Panel 1: Left Rounded Sidebar */}
      <AppSidebar />

      {/* Panel 2: Main Content Rounded Box */}
      <main className="flex-1 bg-card border border-border/80 rounded-3xl overflow-y-auto h-[calc(100vh-2rem)] md:h-[calc(100vh-2.5rem)] shadow-xs relative">
        <Outlet />
      </main>
    </div>
  )
}
