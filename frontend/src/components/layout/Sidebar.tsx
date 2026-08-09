import { NavLink } from "react-router-dom"
import { LayoutPanelLeft, Library, Waypoints, Gauge, PlusCircle } from "lucide-react"
import { Button } from "@/components/ui/button"

const navItems = [
  { name: "Workspace", path: "/workspace", icon: LayoutPanelLeft },
  { name: "Library", path: "/library", icon: Library },
  { name: "Graph Explorer", path: "/graph", icon: Waypoints },
  { name: "Benchmarks", path: "/benchmarks", icon: Gauge },
]

export function Sidebar() {
  return (
    <aside className="w-64 border-r border-sidebar-border bg-sidebar flex flex-col">
      <div className="p-4 border-b border-sidebar-border">
        <Button className="w-full justify-start gap-2 bg-primary text-primary-foreground hover:bg-primary/90">
          <PlusCircle className="h-4 w-4" />
          New Chat
        </Button>
      </div>
      <nav className="flex-1 p-2 space-y-1">
        {navItems.map((item) => (
          <NavLink
            key={item.path}
            to={item.path}
            className={({ isActive }) =>
              `flex items-center gap-3 px-3 py-2 rounded-md transition-colors ${
                isActive
                  ? "bg-secondary text-primary border-l-4 border-primary rounded-l-none"
                  : "text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
              }`
            }
          >
            <item.icon className="h-4 w-4" />
            <span className="font-medium text-sm">{item.name}</span>
          </NavLink>
        ))}
      </nav>
    </aside>
  )
}
