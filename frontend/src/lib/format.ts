const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" })
const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 31536000],
  ["month", 2592000],
  ["day", 86400],
  ["hour", 3600],
  ["minute", 60],
  ["second", 1],
]

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—"
  const diff = (new Date(iso).getTime() - Date.now()) / 1000
  for (const [unit, secs] of UNITS) {
    if (Math.abs(diff) >= secs || unit === "second") {
      return rtf.format(Math.round(diff / secs), unit)
    }
  }
  return "now"
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—"
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  })
}

export function compact(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—"
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(n)
}

export function integer(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—"
  return new Intl.NumberFormat("en").format(n)
}

export function usd(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—"
  return new Intl.NumberFormat("en", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: n < 1 ? 4 : 2,
  }).format(n)
}

export function percent(n: number | null | undefined, digits = 0): string {
  if (n === null || n === undefined) return "—"
  return `${(n * 100).toFixed(digits)}%`
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—"
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`
  if (seconds < 60) return `${seconds.toFixed(1)} s`
  const m = Math.floor(seconds / 60)
  return `${m}m ${Math.round(seconds % 60)}s`
}

export function runDuration(created: string, finished: string | null): number | null {
  if (!finished) return null
  return (new Date(finished).getTime() - new Date(created).getTime()) / 1000
}

export const AGENT_LABEL: Record<string, string> = {
  pr_review: "PR review",
  qa: "Q&A",
}
