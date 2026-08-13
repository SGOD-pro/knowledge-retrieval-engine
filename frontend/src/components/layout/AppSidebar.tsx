import { NavLink } from "react-router-dom"
import {
  FolderKanban,
  FolderOpen,
  BarChart3,
  SlidersHorizontal,
  History,
  Archive
} from "lucide-react"
import { ThemeToggle } from "../common/ThemeToggle"
import { Avatar, AvatarFallback, AvatarImage } from "../ui/avatar"
import { useAuthStore } from "../../store/useAuthStore"
import { useWorkspaceStore } from "../../store/useWorkspaceStore"

export function AppSidebar() {
  const { user } = useAuthStore()
  const { activeWorkspace } = useWorkspaceStore()
  const currentWsId = activeWorkspace?.id || "ws_001"

  const mainNavItems = [
    { name: "Workspace", path: "/workspaces", icon: FolderKanban },
    { name: "Library", path: `/workspaces/${currentWsId}/documents`, icon: FolderOpen },
    { name: "Benchmarks", path: "/benchmarks", icon: BarChart3 },
    { name: "Settings", path: "/settings", icon: SlidersHorizontal }
  ]

  const bottomNavItems = [
    { name: "Chat History", path: `/workspaces/${currentWsId}/chat`, icon: History },
    { name: "Archived", path: "/archived", icon: Archive }
  ]

  return (
    <aside className="w-60 shrink-0 bg-sidebar border border-border/70 rounded-3xl p-5 flex flex-col justify-between h-[calc(100vh-2rem)] md:h-[calc(100vh-2.5rem)] select-none shadow-xs">
      {/* Brand Header */}
      <div>
        <NavLink to="/workspaces" className="flex items-center gap-3 group">
          <div className="h-9 w-9 rounded-lg bg-[#ffffff] dark:bg-[#282a2c] border border-border/70 shadow-xs flex items-center justify-center p-1.5 transition-transform group-hover:scale-105">
            <img
              src="/logo.png"
              alt="KRE Logo"
              className="h-full w-full object-contain"
            />
          </div>
          <div className="flex flex-col">
            <span className="font-headline font-bold text-lg tracking-wider text-primary">
              KRE
            </span>
          </div>
        </NavLink>

        {/* Main Navigation */}
        <nav className="mt-8 space-y-1.5">
          {mainNavItems.map((item) => (
            <NavLink
              key={item.path}
              to={item.path}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3.5 py-2.5 rounded-xl text-sm font-medium transition-all ${
                  isActive
                    ? "bg-[#ede9de] dark:bg-[#282a2c] text-[#c96442] dark:text-[#ffb59d] font-semibold shadow-xs"
                    : "text-sidebar-foreground/80 hover:bg-sidebar-accent/60 hover:text-foreground"
                }`
              }
            >
              <item.icon className="h-4 w-4" />
              <span>{item.name}</span>
            </NavLink>
          ))}
        </nav>
      </div>

      {/* Bottom Section */}
      <div>
        <div className="border-t border-border/60 pt-4 space-y-1.5">
          {bottomNavItems.map((item) => (
            <NavLink
              key={item.name}
              to={item.path}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3.5 py-2 rounded-xl text-sm font-medium transition-all ${
                  isActive
                    ? "text-primary font-semibold bg-[#ede9de]/50 dark:bg-[#282a2c]/50"
                    : "text-sidebar-foreground/75 hover:bg-sidebar-accent/60 hover:text-foreground"
                }`
              }
            >
              <item.icon className="h-4 w-4 opacity-80" />
              <span>{item.name}</span>
            </NavLink>
          ))}
        </div>

        {/* User Profile & Theme Toggle */}
        <div className="mt-5 pt-4 border-t border-border/60 flex items-center justify-between">
          <ThemeToggle />

          <NavLink
            to="/login"
            className="flex items-center gap-2.5 p-1 rounded-full hover:ring-2 hover:ring-primary/40 transition-all"
            title="User Profile / Switch Account"
          >
            <Avatar className="h-9 w-9 border border-border/70">
              <AvatarImage
                src={
                  user?.avatar ||
                  "https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=128&auto=format&fit=crop&q=80"
                }
                alt={user?.name || "User Avatar"}
              />
              <AvatarFallback className="bg-primary/10 text-primary font-medium text-xs">
                {user?.name?.slice(0, 2).toUpperCase() || "AC"}
              </AvatarFallback>
            </Avatar>
          </NavLink>
        </div>
      </div>
    </aside>
  )
}
