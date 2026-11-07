import { useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Link2, Link2Off, ScrollText, Search } from "lucide-react"

import { BlurFade } from "@/components/motion/blur-fade"
import { PageHeader } from "@/components/page-header"
import { EmptyState, ErrorState } from "@/components/states"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import { dateTime, timeAgo } from "@/lib/format"

const head = "font-mono text-[11px] uppercase tracking-wider"
const DECISION = { allow: "success", deny: "destructive", info: "info" } as const

export function AuditPage() {
  const audit = useQuery({ queryKey: ["audit"], queryFn: () => api.audit(500) })
  const [q, setQ] = useState("")
  const [decision, setDecision] = useState("all")
  const entries = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return (audit.data?.entries ?? []).filter(
      (e) =>
        (decision === "all" || e.decision === decision) &&
        (!needle ||
          [e.action, e.actor, e.target ?? "", JSON.stringify(e.details ?? {})]
            .join(" ")
            .toLowerCase()
            .includes(needle)),
    )
  }, [audit.data, q, decision])

  return (
    <>
      <PageHeader
        eyebrow="Audit log"
        title="Who did what"
        description="Append-only and hash-chained: every row includes the hash of the one before, so an edited row breaks the chain."
      />
      {audit.isError && <ErrorState error={audit.error} onRetry={() => audit.refetch()} />}
      {audit.data && (
        <BlurFade
          className={`mb-4 flex items-center gap-3 rounded-md border-[1.5px] p-3 ${
            audit.data.chain_intact ? "border-success/60 bg-success/5" : "border-destructive bg-destructive/5"
          }`}
        >
          {audit.data.chain_intact ? (
            <Link2 className="size-5 text-success" />
          ) : (
            <Link2Off className="size-5 text-destructive" />
          )}
          <p className="text-sm">
            {audit.data.chain_intact
              ? "Hash chain verified: no entry has been altered."
              : `Hash chain broken at entry #${audit.data.first_broken_id}. Investigate before trusting later entries.`}
          </p>
        </BlurFade>
      )}
      <div className="mb-4 flex flex-col gap-2 sm:flex-row">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Filter by action, actor, target or details…"
            className="h-9 border-[1.5px] border-border bg-card pl-9"
            aria-label="Filter audit log"
          />
        </div>
        <Select value={decision} onValueChange={setDecision}>
          <SelectTrigger className="h-9 w-full border-[1.5px] border-border bg-card sm:w-40" aria-label="Decision">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All decisions</SelectItem>
            <SelectItem value="allow">Allowed</SelectItem>
            <SelectItem value="deny">Denied</SelectItem>
            <SelectItem value="info">Info</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <div className="panel overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow className="border-b-[1.5px] border-border">
              <TableHead className={head}>#</TableHead>
              <TableHead className={head}>When</TableHead>
              <TableHead className={head}>Action</TableHead>
              <TableHead className={`${head} hidden md:table-cell`}>Actor</TableHead>
              <TableHead className={`${head} hidden sm:table-cell`}>Target</TableHead>
              <TableHead className={head}>Decision</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {audit.isPending &&
              Array.from({ length: 6 }, (_, i) => (
                <TableRow key={i}>
                  <TableCell colSpan={6}>
                    <Skeleton className="h-5 w-full" />
                  </TableCell>
                </TableRow>
              ))}
            {entries.map((e) => (
              <TableRow key={e.id} className="border-border/30 align-top">
                <TableCell className="num text-xs text-muted-foreground">{e.id}</TableCell>
                <TableCell className="num text-xs whitespace-nowrap" title={dateTime(e.at)}>
                  {timeAgo(e.at)}
                </TableCell>
                <TableCell>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span className="cursor-help font-mono text-xs font-semibold">{e.action}</span>
                    </TooltipTrigger>
                    <TooltipContent className="max-w-sm">
                      <pre className="font-mono text-[11px] whitespace-pre-wrap">
                        {JSON.stringify(e.details ?? {}, null, 2)}
                      </pre>
                      <p className="mt-1 font-mono text-[10px] opacity-70">hash {e.hash.slice(0, 16)}…</p>
                    </TooltipContent>
                  </Tooltip>
                </TableCell>
                <TableCell className="hidden max-w-[14rem] truncate font-mono text-xs md:table-cell">{e.actor}</TableCell>
                <TableCell className="hidden max-w-[16rem] truncate text-xs sm:table-cell">{e.target ?? "—"}</TableCell>
                <TableCell>
                  <Badge variant={DECISION[e.decision]}>{e.decision}</Badge>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {audit.data && entries.length === 0 && (
          <div className="p-4">
            <EmptyState icon={ScrollText} title={q || decision !== "all" ? "No matching entries" : "No entries yet"} />
          </div>
        )}
      </div>
    </>
  )
}
