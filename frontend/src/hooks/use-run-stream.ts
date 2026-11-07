import { useEffect, useRef, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"

export type StreamState = "connecting" | "live" | "offline"

/** Subscribes to /api/runs/stream (server-sent events). Each run event refreshes the
 *  affected queries; bursts are coalesced so a flood of events refetches once. */
export function useRunStream(enabled: boolean): StreamState {
  const qc = useQueryClient()
  const [state, setState] = useState<StreamState>("connecting")
  const pending = useRef<Set<string>>(new Set())
  const timer = useRef<number | null>(null)

  useEffect(() => {
    if (!enabled) return
    const es = new EventSource("/api/runs/stream", { withCredentials: true })
    const flush = () => {
      timer.current = null
      for (const id of pending.current) qc.invalidateQueries({ queryKey: ["run", id] })
      pending.current.clear()
      qc.invalidateQueries({ queryKey: ["runs"] })
      qc.invalidateQueries({ queryKey: ["overview"] })
      qc.invalidateQueries({ queryKey: ["approvals"] })
    }
    es.onopen = () => setState("live")
    es.onerror = () => setState("offline") // EventSource reconnects on its own
    es.addEventListener("run", (e) => {
      try {
        const data = JSON.parse((e as MessageEvent<string>).data) as { run_id: string }
        pending.current.add(data.run_id)
      } catch {
        /* ignore malformed events */
      }
      if (timer.current === null) timer.current = window.setTimeout(flush, 400)
    })
    return () => {
      es.close()
      if (timer.current !== null) window.clearTimeout(timer.current)
    }
  }, [enabled, qc])

  return enabled ? state : "offline"
}
