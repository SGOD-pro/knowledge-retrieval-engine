import { AlertCircle } from "lucide-react"
import { Alert, AlertDescription } from "@/components/ui/alert"

interface NotLiveBadgeProps {
  text?: string
}

export function NotLiveBadge({ text = "Not built yet / only available when live" }: NotLiveBadgeProps) {
  return (
    <Alert variant="default" className="text-muted-foreground border-border bg-transparent shadow-none">
      <AlertCircle className="h-4 w-4 text-muted-foreground" />
      <AlertDescription className="ml-2">
        {text}
      </AlertDescription>
    </Alert>
  )
}
