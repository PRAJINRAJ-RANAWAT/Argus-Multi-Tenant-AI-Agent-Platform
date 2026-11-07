import { useEffect, useMemo, useState } from "react"
import { useNavigate, useParams, useSearchParams } from "react-router"
import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { ArrowDown, ArrowUp, ArrowUpDown, ChevronLeft, ChevronRight, Inbox, Search, X } from "lucide-react"

import { PageHeader } from "@/components/page-header"
import { RunDetailSheet } from "@/components/run-detail"
import { EmptyState, ErrorState } from "@/components/states"
import { StatusBadge } from "@/components/status-badge"
import { STATUS_OPTIONS } from "@/lib/status"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api, type Run } from "@/lib/api"
import { AGENT_LABEL, duration, integer, runDuration, timeAgo } from "@/lib/format"
import { cn } from "@/lib/utils"

type SortKey = "created" | "duration" | "tokens" | "steps"
const PAGE_SIZES = [10, 25, 50]

function sortValue(r: Run, key: SortKey): number {
  switch (key) {
    case "duration":
      return runDuration(r.created_at, r.finished_at) ?? -1
    case "tokens":
      return r.tokens.input + r.tokens.output
    case "steps":
      return r.steps
    default:
      return new Date(r.created_at).getTime()
  }
}

function SortHead({
  label,
  k,
  sort,
  onSort,
  className,
}: {
  label: string
  k: SortKey
  sort: { key: SortKey; dir: "asc" | "desc" }
  onSort: (k: SortKey) => void
  className?: string
}) {
  const active = sort.key === k
  const Icon = !active ? ArrowUpDown : sort.dir === "asc" ? ArrowUp : ArrowDown
  return (
    <TableHead
      className={className}
      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
    >
      <button
        type="button"
        onClick={() => onSort(k)}
        className="inline-flex items-center gap-1 font-mono text-[11px] uppercase tracking-wider hover:text-foreground"
      >
        {label}
        <Icon className={cn("size-3", !active && "opacity-40")} />
      </button>
    </TableHead>
  )
}

export function RunsPage() {
  const [params, setParams] = useSearchParams()
  const { runId } = useParams()
  const navigate = useNavigate()
  const status = params.get("status") ?? ""
  const agent = params.get("agent") ?? ""
  const page = Math.max(0, Number(params.get("page") ?? 0))
  const size = PAGE_SIZES.includes(Number(params.get("size"))) ? Number(params.get("size")) : 25
  const [search, setSearch] = useState(params.get("q") ?? "")
  const [q, setQ] = useState(search)
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({ key: "created", dir: "desc" })

  useEffect(() => {
    const t = window.setTimeout(() => setQ(search.trim()), 300)
    return () => window.clearTimeout(t)
  }, [search])

  const update = (next: Record<string, string>) => {
    const p = new URLSearchParams(params)
    for (const [k, v] of Object.entries(next)) {
      if (v) p.set(k, v)
      else p.delete(k)
    }
    if (!("page" in next)) p.delete("page")
    setParams(p, { replace: true })
  }

  useEffect(() => {
    if (q !== (params.get("q") ?? "")) update({ q })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q])

  const runs = useQuery({
    queryKey: ["runs", { status, agent, q, page, size }],
    queryFn: () => api.runs({ status, agent, q, limit: size, offset: page * size }),
    placeholderData: keepPreviousData,
  })

  const rows = useMemo(() => {
    const list = [...(runs.data?.runs ?? [])]
    list.sort((a, b) => (sortValue(a, sort.key) - sortValue(b, sort.key)) * (sort.dir === "asc" ? 1 : -1))
    return list
  }, [runs.data, sort])

  const total = runs.data?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / size))
  const filtered = !!(status || agent || q)
  const onSort = (k: SortKey) =>
    setSort((s) => ({ key: k, dir: s.key === k && s.dir === "desc" ? "asc" : "desc" }))

  return (
    <>
      <PageHeader
        eyebrow="Runs"
        title="Every review and answer"
        description="Search by repository or question, filter by outcome, and open any run to see what Argus posted and why."
      />

      <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search repository or question…"
            className="h-9 border-[1.5px] border-border bg-card pl-9"
            aria-label="Search runs"
          />
        </div>
        <Select value={status || "all"} onValueChange={(v) => update({ status: v === "all" ? "" : v })}>
          <SelectTrigger className="h-9 w-full border-[1.5px] border-border bg-card sm:w-44" aria-label="Status">
            <SelectValue placeholder="Status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            {STATUS_OPTIONS.map((s) => (
              <SelectItem key={s.value} value={s.value}>
                {s.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={agent || "all"} onValueChange={(v) => update({ agent: v === "all" ? "" : v })}>
          <SelectTrigger className="h-9 w-full border-[1.5px] border-border bg-card sm:w-36" aria-label="Agent">
            <SelectValue placeholder="Agent" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All agents</SelectItem>
            <SelectItem value="pr_review">PR review</SelectItem>
            <SelectItem value="qa">Q&A</SelectItem>
          </SelectContent>
        </Select>
        {filtered && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setSearch("")
              setParams({}, { replace: true })
            }}
          >
            <X /> Clear
          </Button>
        )}
      </div>

      {runs.isError ? (
        <ErrorState error={runs.error} onRetry={() => runs.refetch()} />
      ) : (
        <div className="panel overflow-hidden">
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow className="border-b-[1.5px] border-border hover:bg-transparent">
                  <TableHead className="hidden font-mono text-[11px] uppercase tracking-wider sm:table-cell">Agent</TableHead>
                  <TableHead className="font-mono text-[11px] uppercase tracking-wider">Repository</TableHead>
                  <TableHead className="font-mono text-[11px] uppercase tracking-wider">Status</TableHead>
                  <SortHead label="Steps" k="steps" sort={sort} onSort={onSort} className="hidden text-right md:table-cell" />
                  <SortHead label="Tokens" k="tokens" sort={sort} onSort={onSort} className="hidden text-right md:table-cell" />
                  <SortHead label="Duration" k="duration" sort={sort} onSort={onSort} className="hidden text-right sm:table-cell" />
                  <SortHead label="Created" k="created" sort={sort} onSort={onSort} className="text-right" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.isPending &&
                  Array.from({ length: 8 }, (_, i) => (
                    <TableRow key={i}>
                      <TableCell colSpan={7}>
                        <Skeleton className="h-5 w-full" />
                      </TableCell>
                    </TableRow>
                  ))}
                {rows.map((r) => (
                  <TableRow
                    key={r.id}
                    tabIndex={0}
                    onClick={() => navigate(`/runs/${r.id}?${params.toString()}`)}
                    onKeyDown={(e) => e.key === "Enter" && navigate(`/runs/${r.id}?${params.toString()}`)}
                    className={cn(
                      "cursor-pointer border-border/30 transition-colors focus-visible:bg-muted",
                      runs.isPlaceholderData && "opacity-60",
                      r.id === runId && "bg-muted",
                    )}
                  >
                    <TableCell className="hidden sm:table-cell">
                      <span className="tag">{AGENT_LABEL[r.agent_type] ?? r.agent_type}</span>
                    </TableCell>
                    <TableCell className="max-w-[11rem] sm:max-w-[18rem]">
                      <span className="tag mb-1 sm:hidden">{AGENT_LABEL[r.agent_type] ?? r.agent_type}</span>
                      <p className="truncate font-medium">
                        {r.repo ?? "—"}
                        {r.pr_number ? <span className="text-muted-foreground"> #{r.pr_number}</span> : null}
                      </p>
                      {r.question && (
                        <p className="truncate text-xs text-muted-foreground">“{r.question}”</p>
                      )}
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={r.status} />
                    </TableCell>
                    <TableCell className="num hidden text-right md:table-cell">{r.steps}</TableCell>
                    <TableCell className="num hidden text-right md:table-cell">
                      {integer(r.tokens.input + r.tokens.output)}
                    </TableCell>
                    <TableCell className="num hidden text-right sm:table-cell">
                      {duration(runDuration(r.created_at, r.finished_at))}
                    </TableCell>
                    <TableCell className="num text-right text-xs text-muted-foreground" title={r.created_at}>
                      {timeAgo(r.created_at)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          {runs.data && runs.data.runs.length === 0 && (
            <div className="p-4">
              <EmptyState icon={Inbox} title={filtered ? "No matching runs" : "No runs yet"}>
                {filtered
                  ? "Try a different search or clear the filters."
                  : "Runs appear when a pull request is opened in an installed repository or someone asks a question."}
              </EmptyState>
            </div>
          )}
          <div className="flex flex-col items-center justify-between gap-3 border-t-[1.5px] border-border px-4 py-3 text-sm sm:flex-row">
            <p className="num text-xs text-muted-foreground" aria-live="polite">
              {total ? `${page * size + 1}–${Math.min(total, (page + 1) * size)} of ${integer(total)}` : "0 runs"}
              {sort.key !== "created" && " · sorted within this page"}
            </p>
            <div className="flex items-center gap-2">
              <Select value={String(size)} onValueChange={(v) => update({ size: v })}>
                <SelectTrigger className="h-8 w-24 text-xs" aria-label="Rows per page">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {PAGE_SIZES.map((s) => (
                    <SelectItem key={s} value={String(s)}>
                      {s} / page
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                variant="outline"
                size="icon-sm"
                aria-label="Previous page"
                disabled={page === 0}
                onClick={() => update({ page: String(page - 1) })}
              >
                <ChevronLeft />
              </Button>
              <span className="num text-xs">
                {page + 1} / {pages}
              </span>
              <Button
                variant="outline"
                size="icon-sm"
                aria-label="Next page"
                disabled={page + 1 >= pages}
                onClick={() => update({ page: String(page + 1) })}
              >
                <ChevronRight />
              </Button>
            </div>
          </div>
        </div>
      )}

      <RunDetailSheet
        runId={runId ?? null}
        onOpenChange={(open) => !open && navigate(`/runs?${params.toString()}`)}
      />
    </>
  )
}
