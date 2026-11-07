export const STATUS: Record<
  string,
  { label: string; variant: "success" | "destructive" | "info" | "warning" | "outline" | "neon" }
> = {
  succeeded: { label: "Succeeded", variant: "success" },
  failed: { label: "Failed", variant: "destructive" },
  running: { label: "Running", variant: "neon" },
  queued: { label: "Queued", variant: "info" },
  waiting_approval: { label: "Needs approval", variant: "warning" },
  superseded: { label: "Superseded", variant: "outline" },
  cancelled: { label: "Cancelled", variant: "outline" },
}

export const STATUS_OPTIONS = Object.entries(STATUS).map(([value, s]) => ({ value, label: s.label }))
