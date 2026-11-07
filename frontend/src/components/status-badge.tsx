import { Badge } from "@/components/ui/badge"
import type { RunStatus } from "@/lib/api"
import { STATUS } from "@/lib/status"

export function StatusBadge({ status }: { status: RunStatus | string }) {
  const s = STATUS[status] ?? { label: status, variant: "outline" as const }
  return (
    <Badge variant={s.variant}>
      {status === "running" && (
        <span className="size-1.5 animate-pulse rounded-full bg-current motion-reduce:animate-none" />
      )}
      {s.label}
    </Badge>
  )
}
