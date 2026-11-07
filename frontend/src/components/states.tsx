import { AlertTriangle, RotateCw } from "lucide-react"

import { Button } from "@/components/ui/button"
import { ApiError } from "@/lib/api"
import { cn } from "@/lib/utils"

export function EmptyState({
  icon: Icon,
  title,
  children,
  action,
  className,
}: {
  icon: React.ComponentType<{ className?: string }>
  title: string
  children?: React.ReactNode
  action?: React.ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 rounded-md border-[1.5px] border-dashed border-border/60 px-6 py-12 text-center",
        className,
      )}
    >
      <div className="grid size-10 place-items-center rounded-md border-[1.5px] border-border bg-neon text-neon-foreground shadow-hard-sm">
        <Icon className="size-5" />
      </div>
      <p className="display text-sm">{title}</p>
      {children && <p className="max-w-sm text-sm text-muted-foreground">{children}</p>}
      {action}
    </div>
  )
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message =
    error instanceof ApiError
      ? error.status === 403
        ? `You don't have access to this: ${error.message}`
        : error.message
      : error instanceof Error
        ? error.message
        : "Something went wrong."
  return (
    <div
      role="alert"
      className="flex flex-col items-start gap-3 rounded-md border-[1.5px] border-destructive bg-destructive/5 p-4 sm:flex-row sm:items-center"
    >
      <AlertTriangle className="size-5 shrink-0 text-destructive" />
      <p className="flex-1 text-sm">{message}</p>
      {onRetry && (
        <Button size="sm" variant="outline" onClick={onRetry}>
          <RotateCw /> Retry
        </Button>
      )}
    </div>
  )
}
