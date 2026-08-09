import { ThemeProvider } from "../theme-provider"
import { TooltipProvider } from "@/components/ui/tooltip"
import { Sidebar } from "./Sidebar"
import { Header } from "./Header"
import { Toaster } from "@/components/ui/sonner"

interface ShellProps {
  children: React.ReactNode
}

export function Shell({ children }: ShellProps) {
  return (
    <ThemeProvider attribute="class" defaultTheme="dark">
      <TooltipProvider delayDuration={0}>
        <div className="flex h-screen w-full overflow-hidden bg-background font-sans">
          <Sidebar />
          <div className="flex flex-col flex-1 overflow-hidden">
            <Header />
            <main className="flex-1 overflow-auto relative">
              {children}
            </main>
          </div>
        </div>
        <Toaster />
      </TooltipProvider>
    </ThemeProvider>
  )
}
