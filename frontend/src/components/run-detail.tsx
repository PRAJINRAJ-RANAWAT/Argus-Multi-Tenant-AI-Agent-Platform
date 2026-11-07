import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ExternalLink, ShieldAlert, ShieldCheck, ThumbsDown, ThumbsUp } from "lucide-react"
import { toast } from "sonner"

import { Markdown } from "@/components/markdown"
import { ErrorState } from "@/components/states"
import { StatusBadge } from "@/components/status-badge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api, type Run } from "@/lib/api"
import { AGENT_LABEL, dateTime, duration, integer, runDuration, timeAgo } from "@/lib/format"

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <p className="eyebrow">{label}</p>
      <div className="num mt-0.5 text-sm break-words">{children}</div>
    </div>
  )
}

function Output({ run }: { run: Run }) {
  const r = run.result
  const text = r?.review ?? r?.answer
  if (run.error && !text) {
    return (
      <div className="rounded-md border-[1.5px] border-destructive bg-destructive/5 p-3 font-mono text-xs">
        {run.error}
      </div>
    )
  }
  if (!text) {
    return (
      <p className="text-sm text-muted-foreground">
        {run.status === "queued" || run.status === "running"
          ? "This run is still working. The output appears here when it finishes."
          : run.status === "superseded"
            ? "A newer push replaced this run before it posted."
            : run.status === "waiting_approval"
              ? "Waiting for a member to approve the write action."
              : "No output was recorded for this run."}
      </p>
    )
  }
  return (
    <div className="space-y-4">
      {r?.cache_hit && <Badge variant="info">Answered from the semantic cache</Badge>}
      <Markdown>{text}</Markdown>
      {(r?.citations?.length ?? 0) > 0 && (
        <div>
          <p className="eyebrow mb-2">Verified citations</p>
          <ul className="space-y-1.5">
            {r!.citations!.map((c) => (
              <li key={c.citation}>
                <a
                  href={c.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="group inline-flex items-center gap-1.5 font-mono text-xs underline decoration-neon decoration-2 underline-offset-2"
                >
                  {c.citation}
                  <ExternalLink className="size-3 opacity-60 group-hover:opacity-100" />
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
      {(r?.unverified_citations?.length ?? 0) > 0 && (
        <p className="text-xs text-warning">
          Not in the index (not shown as citations): {r!.unverified_citations!.join(", ")}
        </p>
      )}
    </div>
  )
}

function Guardrails({ run }: { run: Run }) {
  const g = run.result?.guardrails
  if (!g) return <p className="text-sm text-muted-foreground">No guardrail data for this run.</p>
  const redactions = Object.entries(g.output_redactions ?? {})
  return (
    <div className="space-y-4 text-sm">
      <div className="flex items-start gap-3">
        {g.injection_flagged ? (
          <ShieldAlert className="mt-0.5 size-5 shrink-0 text-warning" />
        ) : (
          <ShieldCheck className="mt-0.5 size-5 shrink-0 text-success" />
        )}
        <div>
          <p className="font-semibold">
            {g.injection_flagged ? "Prompt injection suspected" : "No injection detected"}
          </p>
          {g.injection_flagged && (
            <p className="text-muted-foreground">
              The run continued with a reduced tool set and a warning in its output.
            </p>
          )}
        </div>
      </div>
      {(g.rules?.length ?? 0) > 0 && (
        <div>
          <p className="eyebrow mb-1.5">Rules matched</p>
          <div className="flex flex-wrap gap-1.5">
            {g.rules!.map((rule) => (
              <span key={rule} className="tag">
                {rule.replaceAll("_", " ")}
              </span>
            ))}
          </div>
        </div>
      )}
      {(g.sources?.length ?? 0) > 0 && (
        <p className="text-muted-foreground">
          Found in: <span className="font-mono text-xs">{g.sources!.join(", ")}</span>
        </p>
      )}
      <div>
        <p className="eyebrow mb-1.5">Output scanning</p>
        {redactions.length === 0 ? (
          <p className="text-muted-foreground">Nothing redacted.</p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {redactions.map(([kind, n]) => (
              <Badge key={kind} variant="warning">
                {n} {kind} redacted
              </Badge>
            ))}
          </div>
        )}
        {(g.echoes_removed ?? 0) > 0 && (
          <p className="mt-1 text-muted-foreground">
            {g.echoes_removed} line(s) copied from untrusted input were removed.
          </p>
        )}
      </div>
    </div>
  )
}

function Feedback({ run }: { run: Run }) {
  const qc = useQueryClient()
  const vote = useMutation({
    mutationFn: (rating: "up" | "down") => api.feedback(run.id, rating),
    onSuccess: (_, rating) => {
      toast.success(rating === "up" ? "Thanks, marked helpful" : "Thanks, marked not helpful")
      qc.invalidateQueries({ queryKey: ["evals"] })
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Could not save feedback"),
  })
  return (
    <div className="flex items-center gap-2">
      <span className="eyebrow mr-1">Helpful?</span>
      <Button
        size="icon-sm"
        variant="outline"
        aria-label="Helpful"
        disabled={vote.isPending}
        onClick={() => vote.mutate("up")}
      >
        <ThumbsUp />
      </Button>
      <Button
        size="icon-sm"
        variant="outline"
        aria-label="Not helpful"
        disabled={vote.isPending}
        onClick={() => vote.mutate("down")}
      >
        <ThumbsDown />
      </Button>
    </div>
  )
}

export function RunDetailSheet({
  runId,
  onOpenChange,
}: {
  runId: string | null
  onOpenChange: (open: boolean) => void
}) {
  const run = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.run(runId!),
    enabled: !!runId,
  })
  const r = run.data
  return (
    <Sheet open={!!runId} onOpenChange={onOpenChange}>
      <SheetContent className="flex w-full flex-col gap-0 border-l-[1.5px] p-0 sm:max-w-xl">
        <SheetHeader className="border-b-[1.5px] border-border p-5">
          <p className="eyebrow">{r ? (AGENT_LABEL[r.agent_type] ?? r.agent_type) : "Run"}</p>
          <SheetTitle className="display truncate pr-8 text-lg">
            {r ? `${r.repo ?? "—"}${r.pr_number ? ` #${r.pr_number}` : ""}` : "Loading…"}
          </SheetTitle>
          <SheetDescription className="font-mono text-[11px]">{runId}</SheetDescription>
          {r && (
            <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Stat label="Status">
                <StatusBadge status={r.status} />
              </Stat>
              <Stat label="Started">{timeAgo(r.created_at)}</Stat>
              <Stat label="Duration">{duration(runDuration(r.created_at, r.finished_at))}</Stat>
              <Stat label="Tokens">{integer(r.tokens.input + r.tokens.output)}</Stat>
            </div>
          )}
        </SheetHeader>
        <ScrollArea className="min-h-0 flex-1">
          <div className="p-5">
            {run.isPending && (
              <div className="space-y-3">
                <Skeleton className="h-4 w-2/3" />
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-5/6" />
                <Skeleton className="h-24 w-full" />
              </div>
            )}
            {run.isError && <ErrorState error={run.error} onRetry={() => run.refetch()} />}
            {r && (
              <Tabs defaultValue="output">
                <TabsList className="mb-4">
                  <TabsTrigger value="output">{r.agent_type === "qa" ? "Answer" : "Review"}</TabsTrigger>
                  <TabsTrigger value="guardrails">Guardrails</TabsTrigger>
                  <TabsTrigger value="details">Details</TabsTrigger>
                </TabsList>
                <TabsContent value="output">
                  <Output run={r} />
                </TabsContent>
                <TabsContent value="guardrails">
                  <Guardrails run={r} />
                </TabsContent>
                <TabsContent value="details">
                  <div className="grid grid-cols-2 gap-4">
                    <Stat label="Created">{dateTime(r.created_at)}</Stat>
                    <Stat label="Finished">{dateTime(r.finished_at)}</Stat>
                    <Stat label="Steps">{r.steps}</Stat>
                    <Stat label="Replayed steps">{r.result?.steps_replayed ?? 0}</Stat>
                    <Stat label="Model">{r.result?.model ?? "—"}</Stat>
                    <Stat label="Prompt version">{r.result?.prompt_version ?? "—"}</Stat>
                    <Stat label="Input tokens">{integer(r.tokens.input)}</Stat>
                    <Stat label="Output tokens">{integer(r.tokens.output)}</Stat>
                  </div>
                  <p className="eyebrow mt-6 mb-2">Raw result</p>
                  <pre className="max-h-72 overflow-auto rounded-md border border-border/40 bg-muted p-3 font-mono text-[11px]">
                    {JSON.stringify(r.result ?? { error: r.error }, null, 2)}
                  </pre>
                </TabsContent>
              </Tabs>
            )}
          </div>
        </ScrollArea>
        {r && r.status === "succeeded" && (
          <div className="border-t-[1.5px] border-border p-4">
            <Feedback run={r} />
          </div>
        )}
      </SheetContent>
    </Sheet>
  )
}
