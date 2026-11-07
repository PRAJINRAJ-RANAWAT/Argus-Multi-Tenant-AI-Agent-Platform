import { useState } from "react"
import { useNavigate } from "react-router"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { CornerDownLeft, ExternalLink, FolderGit2, Loader2, MessageSquareCode } from "lucide-react"
import { toast } from "sonner"

import { Markdown } from "@/components/markdown"
import { BlurFade } from "@/components/motion/blur-fade"
import { BorderBeam } from "@/components/motion/border-beam"
import { PageHeader } from "@/components/page-header"
import { EmptyState, ErrorState } from "@/components/states"
import { StatusBadge } from "@/components/status-badge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import { api } from "@/lib/api"
import { compact, timeAgo } from "@/lib/format"

const EXAMPLES = [
  "How do we retry failed calls with backoff?",
  "Where are webhook signatures verified?",
  "What calls charge_customer?",
]
const TERMINAL = new Set(["succeeded", "failed", "cancelled", "superseded"])

export function AskPage() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const repos = useQuery({ queryKey: ["repos"], queryFn: api.repos })
  const history = useQuery({
    queryKey: ["runs", "qa-history"],
    queryFn: () => api.runs({ agent: "qa", limit: 8 }),
  })
  const [picked, setPicked] = useState("")
  const [question, setQuestion] = useState("")
  const [runId, setRunId] = useState<string | null>(null)

  // Default to the first indexed repository until the user picks one.
  const fallback = (repos.data?.repos.find((r) => r.chunks > 0) ?? repos.data?.repos[0])?.full_name ?? ""
  const repo = picked || fallback

  const ask = useMutation({
    mutationFn: () => api.ask(repo, question.trim()),
    onSuccess: (res) => {
      setRunId(res.run_id)
      qc.invalidateQueries({ queryKey: ["runs"] })
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Could not ask"),
  })

  const run = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.run(runId!),
    enabled: !!runId,
    refetchInterval: (q) => (q.state.data && TERMINAL.has(q.state.data.status) ? false : 1500),
  })

  const selected = repos.data?.repos.find((r) => r.full_name === repo)
  const canSubmit = !!repo && question.trim().length > 2 && !ask.isPending
  const working = ask.isPending || (!!runId && !!run.data && !TERMINAL.has(run.data.status))
  const answer = run.data?.result?.answer

  const submit = (e?: React.FormEvent) => {
    e?.preventDefault()
    if (canSubmit) ask.mutate()
  }

  return (
    <>
      <PageHeader
        eyebrow="Ask the code"
        title="Codebase Q&A"
        description="Answers come from the indexed code of a repository you can read, with every claim cited to file and line."
      />

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <BlurFade>
            <form onSubmit={submit} className="panel space-y-4 p-4 sm:p-5">
              <div className="grid gap-4 sm:grid-cols-[1fr_auto] sm:items-end">
                <div className="space-y-1.5">
                  <Label htmlFor="repo" className="eyebrow">
                    Repository
                  </Label>
                  {repos.isPending ? (
                    <Skeleton className="h-9 w-full" />
                  ) : (
                    <Select value={repo} onValueChange={setPicked}>
                      <SelectTrigger id="repo" className="h-9 w-full border-[1.5px] border-border bg-background">
                        <SelectValue placeholder="Choose a repository" />
                      </SelectTrigger>
                      <SelectContent>
                        {repos.data?.repos.map((r) => (
                          <SelectItem key={r.id} value={r.full_name}>
                            {r.full_name}
                            <span className="ml-2 font-mono text-[11px] text-muted-foreground">
                              {r.chunks ? `${compact(r.chunks)} chunks` : "not indexed"}
                            </span>
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                </div>
                {selected && (
                  <div className="flex flex-wrap gap-1.5">
                    {selected.private && <Badge variant="outline">private</Badge>}
                    {selected.indexed_commit ? (
                      <Badge variant="success" title={selected.indexed_commit}>
                        indexed @ {selected.indexed_commit.slice(0, 7)}
                      </Badge>
                    ) : (
                      <Badge variant="warning">not indexed yet</Badge>
                    )}
                  </div>
                )}
              </div>
              {repos.isError && <ErrorState error={repos.error} onRetry={() => repos.refetch()} />}
              {repos.data?.repos.length === 0 && (
                <EmptyState icon={FolderGit2} title="No repositories">
                  Install the Argus GitHub App on a repository; it is indexed automatically.
                </EmptyState>
              )}
              <div className="space-y-1.5">
                <Label htmlFor="question" className="eyebrow">
                  Question
                </Label>
                <Textarea
                  id="question"
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit()
                  }}
                  placeholder="How does the billing retry work?"
                  rows={3}
                  maxLength={2000}
                  className="resize-none border-[1.5px] border-border bg-background text-sm"
                />
              </div>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap gap-1.5">
                  {EXAMPLES.map((ex) => (
                    <button
                      key={ex}
                      type="button"
                      className="tag cursor-pointer normal-case transition-colors hover:border-border hover:bg-neon hover:text-neon-foreground"
                      onClick={() => setQuestion(ex)}
                    >
                      {ex}
                    </button>
                  ))}
                </div>
                <Button type="submit" variant="neon" bracket disabled={!canSubmit}>
                  {ask.isPending ? <Loader2 className="animate-spin" /> : <CornerDownLeft />}
                  Ask
                </Button>
              </div>
            </form>
          </BlurFade>

          {runId && (
            <BlurFade>
              <Card className="relative">
                {working && <BorderBeam duration={3} />}
                <CardHeader className="flex flex-row items-start justify-between gap-3">
                  <div className="min-w-0">
                    <CardTitle className="display text-sm">Answer</CardTitle>
                    <CardDescription className="truncate">{run.data?.repo ?? repo}</CardDescription>
                  </div>
                  {run.data && <StatusBadge status={run.data.status} />}
                </CardHeader>
                <CardContent className="space-y-4" aria-live="polite">
                  {(working || run.isPending) && (
                    <div className="space-y-2">
                      <p className="flex items-center gap-2 text-sm text-muted-foreground">
                        <Loader2 className="size-4 animate-spin" /> Searching the code and drafting an
                        answer…
                      </p>
                      <Skeleton className="h-4 w-full" />
                      <Skeleton className="h-4 w-4/5" />
                    </div>
                  )}
                  {run.isError && <ErrorState error={run.error} onRetry={() => run.refetch()} />}
                  {run.data?.status === "failed" && (
                    <ErrorState error={new Error(run.data.error ?? "The run failed.")} />
                  )}
                  {answer && <Markdown>{answer}</Markdown>}
                  {run.data?.result?.cache_hit && <Badge variant="info">From the semantic cache</Badge>}
                  {(run.data?.result?.citations?.length ?? 0) > 0 && (
                    <div className="flex flex-wrap gap-2">
                      {run.data!.result!.citations!.map((c) => (
                        <a
                          key={c.citation}
                          href={c.url}
                          target="_blank"
                          rel="noreferrer noopener"
                          className="tag panel-interactive bg-card shadow-hard-sm"
                        >
                          {c.citation}
                          <ExternalLink className="size-3" />
                        </a>
                      ))}
                    </div>
                  )}
                </CardContent>
              </Card>
            </BlurFade>
          )}
        </div>

        <BlurFade delay={0.1}>
          <Card>
            <CardHeader>
              <CardTitle className="display text-sm">Recent questions</CardTitle>
              <CardDescription>Across your organisation</CardDescription>
            </CardHeader>
            <CardContent>
              {history.isPending && <Skeleton className="h-32 w-full" />}
              {history.data?.runs.length === 0 && (
                <EmptyState icon={MessageSquareCode} title="No questions yet" className="py-8" />
              )}
              <ul className="space-y-1">
                {history.data?.runs.map((r) => (
                  <li key={r.id}>
                    <button
                      type="button"
                      onClick={() => navigate(`/runs/${r.id}`)}
                      className="w-full rounded-sm px-2 py-2 text-left transition-colors hover:bg-muted"
                    >
                      <p className="line-clamp-2 text-sm font-medium">{r.question ?? r.repo}</p>
                      <p className="mt-0.5 flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
                        {r.repo} · {timeAgo(r.created_at)}
                      </p>
                    </button>
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>
        </BlurFade>
      </div>
    </>
  )
}
