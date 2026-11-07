import { useState } from "react"
import { Link, useNavigate } from "react-router"
import { useQuery } from "@tanstack/react-query"
import {
  Activity,
  ArrowUpRight,
  CheckCheck,
  Clock,
  Gauge,
  Inbox,
  ShieldAlert,
  Sparkles,
} from "lucide-react"
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts"

import { BlurFade } from "@/components/motion/blur-fade"
import { BorderBeam } from "@/components/motion/border-beam"
import { NumberTicker } from "@/components/motion/number-ticker"
import { SpotlightCard } from "@/components/motion/spotlight-card"
import { PageHeader } from "@/components/page-header"
import { EmptyState, ErrorState } from "@/components/states"
import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api } from "@/lib/api"
import { AGENT_LABEL, compact, duration, percent, timeAgo } from "@/lib/format"

const RANGES = [7, 14, 30, 90] as const

const chartConfig = {
  succeeded: { label: "Succeeded", color: "var(--chart-1)" },
  failed: { label: "Failed", color: "var(--chart-5)" },
  other: { label: "Other", color: "var(--chart-2)" },
} satisfies ChartConfig

function Kpi({
  label,
  icon: Icon,
  value,
  format,
  hint,
  to,
}: {
  label: string
  icon: React.ComponentType<{ className?: string }>
  value: number | null
  format?: (n: number) => string
  hint?: React.ReactNode
  to?: string
}) {
  const navigate = useNavigate()
  return (
    <SpotlightCard
      className={to ? "cursor-pointer p-4" : "p-4"}
      onClick={to ? () => navigate(to) : undefined}
      role={to ? "link" : undefined}
      tabIndex={to ? 0 : undefined}
      onKeyDown={to ? (e) => e.key === "Enter" && navigate(to) : undefined}
    >
      <div className="flex items-start justify-between gap-2">
        <p className="eyebrow">{label}</p>
        <Icon className="size-4 text-muted-foreground" />
      </div>
      <p className="mt-3 text-3xl font-semibold leading-none">
        {value === null ? (
          <span className="num text-muted-foreground">—</span>
        ) : (
          <NumberTicker value={value} format={format} />
        )}
      </p>
      {hint && <p className="mt-2 truncate text-xs text-muted-foreground">{hint}</p>}
    </SpotlightCard>
  )
}

function KpiSkeleton() {
  return (
    <div className="panel p-4">
      <Skeleton className="h-3 w-20" />
      <Skeleton className="mt-4 h-8 w-24" />
      <Skeleton className="mt-3 h-3 w-32" />
    </div>
  )
}

export function OverviewPage() {
  const [days, setDays] = useState<number>(14)
  const navigate = useNavigate()
  const overview = useQuery({ queryKey: ["overview", days], queryFn: () => api.overview(days) })
  const recent = useQuery({ queryKey: ["runs", "recent"], queryFn: () => api.runs({ limit: 8 }) })
  const evals = useQuery({ queryKey: ["evals", days], queryFn: () => api.onlineEvals(days) })

  const o = overview.data
  const t = o?.totals
  const judge = evals.data?.judge_by_prompt[0]
  const fb = evals.data?.feedback

  return (
    <>
      <PageHeader
        eyebrow="Overview"
        title="What Argus did"
        description="Reviews and answers across the repositories you can read, live as runs finish."
        actions={
          <Tabs value={String(days)} onValueChange={(v) => setDays(Number(v))}>
            <TabsList aria-label="Time range">
              {RANGES.map((d) => (
                <TabsTrigger key={d} value={String(d)} className="font-mono text-xs">
                  {d}d
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        }
      />

      {overview.isError ? (
        <ErrorState error={overview.error} onRetry={() => overview.refetch()} />
      ) : (
        <BlurFade delay={0.05} className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {!t || !o ? (
            Array.from({ length: 4 }, (_, i) => <KpiSkeleton key={i} />)
          ) : (
            <>
              <Kpi
                label="Runs"
                icon={Activity}
                value={t.runs}
                hint={`${t.reviews} reviews · ${t.questions} questions`}
                to="/runs"
              />
              <Kpi
                label="Success rate"
                icon={Gauge}
                value={o.success_rate === null ? null : o.success_rate * 100}
                format={(n) => `${n.toFixed(0)}%`}
                hint={`${t.failed} failed · ${t.superseded} superseded`}
                to="/runs?status=failed"
              />
              <Kpi
                label="Median duration"
                icon={Clock}
                value={t.p50_s}
                format={(n) => duration(n)}
                hint={`p95 ${duration(t.p95_s)}`}
              />
              <Kpi
                label="Needs approval"
                icon={CheckCheck}
                value={o.pending_approvals}
                hint={`${t.active} running or queued`}
                to="/approvals"
              />
            </>
          )}
        </BlurFade>
      )}

      <div className="mt-6 grid gap-6 lg:grid-cols-3">
        <BlurFade delay={0.1} className="lg:col-span-2">
          <Card className="h-full">
            <CardHeader>
              <CardTitle className="display text-sm">Runs per day</CardTitle>
              <CardDescription>By outcome, last {days} days (UTC)</CardDescription>
            </CardHeader>
            <CardContent>
              {overview.isPending ? (
                <Skeleton className="h-64 w-full" />
              ) : (o?.daily.length ?? 0) === 0 ? (
                <EmptyState icon={Inbox} title="No runs in this range">
                  Open a pull request in an installed repository, or ask the codebase a question.
                </EmptyState>
              ) : (
                <ChartContainer config={chartConfig} className="h-64 w-full">
                  <BarChart data={o!.daily} margin={{ left: -16, right: 8, top: 8 }} maxBarSize={56}>
                    <CartesianGrid vertical={false} strokeDasharray="3 3" />
                    <XAxis
                      dataKey="day"
                      tickLine={false}
                      axisLine={false}
                      tickMargin={8}
                      tickFormatter={(d: string) => d.slice(5)}
                      className="font-mono"
                    />
                    <YAxis allowDecimals={false} tickLine={false} axisLine={false} width={40} />
                    <ChartTooltip content={<ChartTooltipContent />} />
                    <ChartLegend content={<ChartLegendContent />} />
                    <Bar dataKey="succeeded" stackId="a" fill="var(--color-succeeded)" />
                    <Bar dataKey="failed" stackId="a" fill="var(--color-failed)" />
                    <Bar dataKey="other" stackId="a" fill="var(--color-other)" radius={[3, 3, 0, 0]} />
                  </BarChart>
                </ChartContainer>
              )}
            </CardContent>
          </Card>
        </BlurFade>

        <BlurFade delay={0.15}>
          <Card className="h-full">
            <CardHeader>
              <CardTitle className="display text-sm">Top repositories</CardTitle>
              <CardDescription>Runs and success in range</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {overview.isPending &&
                Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-8 w-full" />)}
              {o && o.top_repos.length === 0 && (
                <p className="text-sm text-muted-foreground">No repository activity yet.</p>
              )}
              {o?.top_repos.map((repo) => {
                const max = o.top_repos[0]?.runs || 1
                return (
                  <div key={repo.full_name}>
                    <div className="mb-1 flex items-baseline justify-between gap-2 text-sm">
                      <span className="truncate font-medium">{repo.full_name}</span>
                      <span className="num text-xs text-muted-foreground">
                        {repo.runs} · {percent(repo.runs ? repo.succeeded / repo.runs : null)}
                      </span>
                    </div>
                    <div className="h-2 overflow-hidden rounded-sm border border-border/50 bg-muted">
                      <div
                        className="h-full bg-neon transition-[width] duration-500"
                        style={{ width: `${(repo.runs / max) * 100}%` }}
                      />
                    </div>
                  </div>
                )
              })}
            </CardContent>
          </Card>
        </BlurFade>
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-3">
        <BlurFade delay={0.2} className="lg:col-span-2">
          <Card className="relative h-full">
            <BorderBeam duration={8} />
            <CardHeader className="flex flex-row items-center justify-between">
              <div>
                <CardTitle className="display text-sm">Live activity</CardTitle>
                <CardDescription>Latest runs; updates as they happen</CardDescription>
              </div>
              <Button asChild variant="outline" size="sm">
                <Link to="/runs">
                  All runs <ArrowUpRight />
                </Link>
              </Button>
            </CardHeader>
            <CardContent>
              {recent.isError && <ErrorState error={recent.error} onRetry={() => recent.refetch()} />}
              {recent.isPending && (
                <div className="space-y-3">
                  {Array.from({ length: 5 }, (_, i) => (
                    <Skeleton key={i} className="h-10 w-full" />
                  ))}
                </div>
              )}
              {recent.data && recent.data.runs.length === 0 && (
                <EmptyState icon={Activity} title="Nothing yet">
                  Runs show up here the moment a webhook arrives.
                </EmptyState>
              )}
              <ul className="divide-y divide-border/30">
                {recent.data?.runs.map((r) => (
                  <li key={r.id}>
                    <button
                      type="button"
                      onClick={() => navigate(`/runs/${r.id}`)}
                      className="flex w-full items-center gap-3 rounded-sm px-1 py-2.5 text-left transition-colors hover:bg-muted/60 focus-visible:bg-muted/60"
                    >
                      <span className="tag hidden shrink-0 sm:inline-flex">
                        {AGENT_LABEL[r.agent_type] ?? r.agent_type}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-sm font-medium">
                        {r.repo ?? "—"}
                        {r.pr_number ? <span className="text-muted-foreground"> #{r.pr_number}</span> : null}
                      </span>
                      <span className="num hidden text-xs text-muted-foreground sm:inline">
                        {timeAgo(r.created_at)}
                      </span>
                      <StatusBadge status={r.status} />
                    </button>
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>
        </BlurFade>

        <BlurFade delay={0.25}>
          <Card className="h-full">
            <CardHeader>
              <CardTitle className="display text-sm">Quality & safety</CardTitle>
              <CardDescription>Online judge, user feedback, guardrails</CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <div className="flex items-center gap-3">
                <Sparkles className="size-5 text-muted-foreground" />
                <div className="flex-1">
                  <p className="eyebrow">Judge score (latest prompt)</p>
                  <p className="num text-xl font-semibold">
                    {judge ? `${judge.mean_score.toFixed(2)} / 5` : "—"}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {judge ? `${judge.samples} sampled reviews · prompt ${judge.prompt_version}` : "No samples yet"}
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-3">
                <ThumbsBar up={fb?.up ?? 0} down={fb?.down ?? 0} />
              </div>
              <div className="flex items-center gap-3">
                <ShieldAlert className="size-5 text-muted-foreground" />
                <div>
                  <p className="eyebrow">Injection attempts flagged</p>
                  <p className="num text-xl font-semibold">{t ? compact(t.flagged) : "—"}</p>
                </div>
              </div>
            </CardContent>
          </Card>
        </BlurFade>
      </div>
    </>
  )
}

function ThumbsBar({ up, down }: { up: number; down: number }) {
  const total = up + down
  return (
    <div className="w-full">
      <div className="flex items-baseline justify-between">
        <p className="eyebrow">User feedback</p>
        <p className="num text-xs text-muted-foreground">
          {total ? `${up} 👍 · ${down} 👎` : "No votes yet"}
        </p>
      </div>
      <div
        className="mt-1.5 flex h-2 overflow-hidden rounded-sm border border-border/50 bg-muted"
        role="img"
        aria-label={`${up} helpful, ${down} not helpful`}
      >
        {total > 0 && (
          <>
            <div className="bg-success" style={{ width: `${(up / total) * 100}%` }} />
            <div className="bg-destructive" style={{ width: `${(down / total) * 100}%` }} />
          </>
        )}
      </div>
    </div>
  )
}
