import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { BarChart3, Coins, Cpu, Gauge, Save } from "lucide-react"
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts"
import { toast } from "sonner"

import { BlurFade } from "@/components/motion/blur-fade"
import { NumberTicker } from "@/components/motion/number-ticker"
import { SpotlightCard } from "@/components/motion/spotlight-card"
import { PageHeader } from "@/components/page-header"
import { EmptyState, ErrorState } from "@/components/states"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Slider } from "@/components/ui/slider"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api } from "@/lib/api"
import { compact, integer, percent, usd } from "@/lib/format"
import { useCan } from "@/lib/session"
import { cn } from "@/lib/utils"

const tokensConfig = { used: { label: "Tokens", color: "var(--chart-1)" } } satisfies ChartConfig
const costConfig = { cost_usd: { label: "Cost (USD)", color: "var(--chart-3)" } } satisfies ChartConfig

// The budget slider moves on a log scale: 10k … 1B tokens per day.
const toTokens = (v: number) => Math.round(10 ** v / 1000) * 1000
const toSlider = (t: number) => Math.log10(Math.max(10_000, t))

function BudgetEditor({ current, alertPct }: { current: number | null; alertPct: number }) {
  const qc = useQueryClient()
  // Remounted (via key) when the saved budget changes, so no prop-to-state syncing.
  const [scale, setScale] = useState(toSlider(current ?? 1_000_000))
  const [alert, setAlert] = useState(alertPct)
  const tokens = toTokens(scale)
  const save = useMutation({
    mutationFn: () => api.setBudget(tokens, alert),
    onSuccess: () => {
      toast.success(`Daily budget set to ${compact(tokens)} tokens`)
      qc.invalidateQueries({ queryKey: ["usage"] })
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Could not save the budget"),
  })
  return (
    <div className="space-y-6">
      <div className="space-y-3">
        <div className="flex items-baseline justify-between">
          <Label className="eyebrow">Daily token budget</Label>
          <span className="num text-lg font-semibold">{compact(tokens)}</span>
        </div>
        <Slider
          min={4}
          max={9}
          step={0.01}
          value={[scale]}
          onValueChange={([v]) => setScale(v)}
          aria-label="Daily token budget"
        />
        <div className="num flex justify-between text-[11px] text-muted-foreground">
          <span>10K</span>
          <span>1M</span>
          <span>1B</span>
        </div>
      </div>
      <div className="space-y-3">
        <div className="flex items-baseline justify-between">
          <Label className="eyebrow">Alert at</Label>
          <span className="num text-lg font-semibold">{alert}%</span>
        </div>
        <Slider min={10} max={100} step={5} value={[alert]} onValueChange={([v]) => setAlert(v)} aria-label="Alert threshold" />
      </div>
      <p className="text-xs text-muted-foreground">
        Runs that would exceed the budget stop instead of retrying. Every call reserves its estimate first, so
        concurrent runs cannot overspend.
      </p>
      <Button variant="neon" bracket onClick={() => save.mutate()} disabled={save.isPending}>
        <Save /> Save budget
      </Button>
    </div>
  )
}

export function UsagePage() {
  const [days, setDays] = useState(14)
  const canManage = useCan("admin")
  const usage = useQuery({ queryKey: ["usage", days], queryFn: () => api.usage(days) })
  const u = usage.data
  const daily = [...(u?.daily ?? [])].reverse()
  const today = u?.daily[0]
  const budget = u?.budget
  const calls = u?.models.reduce((s, m) => s + m.calls, 0) ?? 0
  const errors = u?.models.reduce((s, m) => s + m.errors, 0) ?? 0
  const cost = u?.daily.reduce((s, d) => s + d.cost_usd, 0) ?? 0
  const cached = u?.models.reduce((s, m) => s + m.cached_tokens, 0) ?? 0
  const input = u?.models.reduce((s, m) => s + m.input_tokens, 0) ?? 0
  const usedPct = budget && today ? today.used / budget.daily_tokens : null

  return (
    <>
      <PageHeader
        eyebrow="Usage & budget"
        title="Tokens, cost and limits"
        description="Every model call goes through the gateway, which meters it against your organisation's daily budget."
        actions={
          <Tabs value={String(days)} onValueChange={(v) => setDays(Number(v))}>
            <TabsList aria-label="Time range">
              {[7, 14, 30, 90].map((d) => (
                <TabsTrigger key={d} value={String(d)} className="font-mono text-xs">
                  {d}d
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        }
      />
      {usage.isError && <ErrorState error={usage.error} onRetry={() => usage.refetch()} />}

      <BlurFade className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {usage.isPending ? (
          Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-28" />)
        ) : (
          <>
            <SpotlightCard className="p-4">
              <div className="flex justify-between">
                <p className="eyebrow">Today</p>
                <Gauge className="size-4 text-muted-foreground" />
              </div>
              <p className="mt-3 text-3xl font-semibold leading-none">
                <NumberTicker value={today?.used ?? 0} format={(n) => compact(n)} />
              </p>
              <div className="mt-3 h-2 overflow-hidden rounded-sm border border-border/50 bg-muted">
                <div
                  className={cn(
                    "h-full transition-[width] duration-500",
                    usedPct !== null && usedPct * 100 >= (budget?.alert_pct ?? 80) ? "bg-warning" : "bg-neon",
                  )}
                  style={{ width: `${Math.min(100, (usedPct ?? 0) * 100)}%` }}
                />
              </div>
              <p className="mt-1.5 text-xs text-muted-foreground">
                {budget ? `${percent(usedPct, 1)} of ${compact(budget.daily_tokens)} budget` : "No budget set (unlimited)"}
              </p>
            </SpotlightCard>
            <SpotlightCard className="p-4">
              <div className="flex justify-between">
                <p className="eyebrow">Cost ({days}d)</p>
                <Coins className="size-4 text-muted-foreground" />
              </div>
              <p className="mt-3 text-3xl font-semibold leading-none">
                <NumberTicker value={cost} format={(n) => usd(n)} />
              </p>
              <p className="mt-2 text-xs text-muted-foreground">Mock provider prices in the demo</p>
            </SpotlightCard>
            <SpotlightCard className="p-4">
              <div className="flex justify-between">
                <p className="eyebrow">Model calls</p>
                <Cpu className="size-4 text-muted-foreground" />
              </div>
              <p className="mt-3 text-3xl font-semibold leading-none">
                <NumberTicker value={calls} />
              </p>
              <p className="mt-2 text-xs text-muted-foreground">
                {percent(calls ? errors / calls : null, 1)} errors (retried or failed over)
              </p>
            </SpotlightCard>
            <SpotlightCard className="p-4">
              <div className="flex justify-between">
                <p className="eyebrow">Prompt cache</p>
                <BarChart3 className="size-4 text-muted-foreground" />
              </div>
              <p className="mt-3 text-3xl font-semibold leading-none">
                <NumberTicker value={input ? (cached / input) * 100 : 0} format={(n) => `${n.toFixed(0)}%`} />
              </p>
              <p className="mt-2 text-xs text-muted-foreground">of input tokens served from cache</p>
            </SpotlightCard>
          </>
        )}
      </BlurFade>

      <div className="mt-6 grid gap-6 lg:grid-cols-3">
        <BlurFade delay={0.08} className="lg:col-span-2 space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="display text-sm">Tokens per day</CardTitle>
              <CardDescription>Input and output, all models (UTC days)</CardDescription>
            </CardHeader>
            <CardContent>
              {usage.isPending ? (
                <Skeleton className="h-56 w-full" />
              ) : daily.length === 0 ? (
                <EmptyState icon={BarChart3} title="No usage yet">
                  Usage appears after the first model call.
                </EmptyState>
              ) : (
                <ChartContainer config={tokensConfig} className="h-56 w-full">
                  <BarChart data={daily} margin={{ left: -8, right: 8, top: 8 }} maxBarSize={56}>
                    <CartesianGrid vertical={false} strokeDasharray="3 3" />
                    <XAxis dataKey="day" tickLine={false} axisLine={false} tickMargin={8} tickFormatter={(d: string) => d.slice(5)} />
                    <YAxis tickLine={false} axisLine={false} width={48} tickFormatter={(v: number) => compact(v)} />
                    <ChartTooltip content={<ChartTooltipContent />} />
                    <Bar dataKey="used" fill="var(--color-used)" radius={[3, 3, 0, 0]} />
                  </BarChart>
                </ChartContainer>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="display text-sm">By model</CardTitle>
              <CardDescription>Calls, tokens and cost in range</CardDescription>
            </CardHeader>
            <CardContent className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="font-mono text-[11px] uppercase">Model</TableHead>
                    <TableHead className="text-right font-mono text-[11px] uppercase">Calls</TableHead>
                    <TableHead className="hidden text-right font-mono text-[11px] uppercase sm:table-cell">Input</TableHead>
                    <TableHead className="hidden text-right font-mono text-[11px] uppercase sm:table-cell">Output</TableHead>
                    <TableHead className="text-right font-mono text-[11px] uppercase">Errors</TableHead>
                    <TableHead className="text-right font-mono text-[11px] uppercase">Cost</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {u?.models.map((m) => (
                    <TableRow key={m.model}>
                      <TableCell className="font-mono text-xs">{m.model}</TableCell>
                      <TableCell className="num text-right">{integer(m.calls)}</TableCell>
                      <TableCell className="num hidden text-right sm:table-cell">{compact(m.input_tokens)}</TableCell>
                      <TableCell className="num hidden text-right sm:table-cell">{compact(m.output_tokens)}</TableCell>
                      <TableCell className={cn("num text-right", m.errors > 0 && "text-warning")}>{integer(m.errors)}</TableCell>
                      <TableCell className="num text-right">{usd(m.cost_usd)}</TableCell>
                    </TableRow>
                  ))}
                  {u?.models.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={6} className="py-6 text-center text-sm text-muted-foreground">
                        No model calls in this range.
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </BlurFade>

        <BlurFade delay={0.12} className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="display text-sm">Budget</CardTitle>
              <CardDescription>
                {canManage ? "Per-organisation daily cap" : "Only admins can change the budget"}
              </CardDescription>
            </CardHeader>
            <CardContent>
              {usage.isPending ? (
                <Skeleton className="h-48" />
              ) : canManage ? (
                <BudgetEditor
                  key={`${budget?.daily_tokens}-${budget?.alert_pct}`}
                  current={budget?.daily_tokens ?? null}
                  alertPct={budget?.alert_pct ?? 80}
                />
              ) : (
                <p className="num text-2xl font-semibold">
                  {budget ? `${compact(budget.daily_tokens)} / day` : "Unlimited"}
                </p>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle className="display text-sm">Daily cost</CardTitle>
            </CardHeader>
            <CardContent>
              {daily.length > 0 ? (
                <ChartContainer config={costConfig} className="h-36 w-full">
                  <BarChart data={daily} margin={{ left: -8, right: 8 }} maxBarSize={40}>
                    <XAxis dataKey="day" tickLine={false} axisLine={false} tickFormatter={(d: string) => d.slice(5)} />
                    <YAxis hide />
                    <ChartTooltip content={<ChartTooltipContent />} />
                    <Bar dataKey="cost_usd" fill="var(--color-cost_usd)" radius={[3, 3, 0, 0]} />
                  </BarChart>
                </ChartContainer>
              ) : (
                <p className="text-sm text-muted-foreground">No cost recorded yet.</p>
              )}
            </CardContent>
          </Card>
        </BlurFade>
      </div>
    </>
  )
}
