import { Sun, Moon } from "lucide-react"
import { useTheme } from "../../contexts/ThemeProvider"
import { Button } from "../ui/button"

export function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme()

  const isDark =
    theme === "dark" ||
    (theme === "system" &&
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches)

  const toggleTheme = () => {
    setTheme(isDark ? "light" : "dark")
  }

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={toggleTheme}
      className={`h-9 w-9 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent ${className}`}
      title={isDark ? "Switch to Light theme" : "Switch to Dark theme"}
    >
      {isDark ? (
        <Sun className="h-4 w-4 text-[#ffb59d] transition-transform duration-300 rotate-0 hover:rotate-45" />
      ) : (
        <Moon className="h-4 w-4 text-[#c96442] transition-transform duration-300 -rotate-12 hover:rotate-0" />
      )}
      <span className="sr-only">Toggle theme</span>
    </Button>
  )
}
