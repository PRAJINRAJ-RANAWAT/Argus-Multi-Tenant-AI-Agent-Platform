import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, CheckCheck, X } from "lucide-react"
import { toast } from "sonner"

import { Markdown } from "@/components/markdown"
import { BlurFade } from "@/components/motion/blur-fade"
import { PageHeader } from "@/components/page-header"
import { EmptyState, ErrorState } from "@/components/states"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { api, type Approval } from "@/lib/api"
import { timeAgo } from "@/lib/format"
import { useCan } from "@/lib/session"

export function ApprovalsPage() {
  const [tab, setTab] = useState("pending")
  const qc = useQueryClient()
  const canDecide = useCan("member")
  const approvals = useQuery({ queryKey: ["approvals", tab], queryFn: () => api.approvals(tab) })
  const [pending, setPending] = useState<{ a: Approval; decision: "approve" | "reject" } | null>(null)
  const [reason, setReason] = useState("")

  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "reject" }) =>
      api.decide(id, decision, reason),
    onSuccess: (_, v) => {
      toast.success(v.decision === "approve" ? "Approved: the run resumes now" : "Rejected: the run is cancelled")
      qc.invalidateQueries({ queryKey: ["approvals"] })
      qc.invalidateQueries({ queryKey: ["runs"] })
      setPending(null)
      setReason("")
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Decision failed"),
  })

  return (
    <>
      <PageHeader
        eyebrow="Approvals"
        title="Human in the loop"
        description="Write actions your organisation marked for approval wait here. The run holds no worker while it waits; a decision resumes it."
        actions={
          <Tabs value={tab} onValueChange={setTab}>
            <TabsList>
              <TabsTrigger value="pending">Pending</TabsTrigger>
              <TabsTrigger value="approved">Approved</TabsTrigger>
              <TabsTrigger value="rejected">Rejected</TabsTrigger>
            </TabsList>
          </Tabs>
        }
      />

      {approvals.isError && <ErrorState error={approvals.error} onRetry={() => approvals.refetch()} />}
      {approvals.isPending && (
        <div className="grid gap-4 md:grid-cols-2">
          <Skeleton className="h-56" />
          <Skeleton className="h-56" />
        </div>
      )}
      {approvals.data?.approvals.length === 0 && (
        <EmptyState icon={CheckCheck} title={tab === "pending" ? "Nothing to decide" : `No ${tab} actions`}>
          {tab === "pending"
            ? "Turn on approvals for a write tool in Settings to review comments before they are posted."
            : "Decisions show up here with who made them."}
        </EmptyState>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        {approvals.data?.approvals.map((a, i) => (
          <BlurFade key={a.id} delay={i * 0.04}>
            <Card className="h-full">
              <CardHeader>
                <div className="flex items-center justify-between gap-2">
                  <span className="tag">{a.tool.replaceAll("_", " ")}</span>
                  <span className="num text-xs text-muted-foreground">{timeAgo(a.requested_at)}</span>
                </div>
                <CardTitle className="truncate text-base font-semibold">
                  {a.preview.repo ?? a.repo}
                  {a.preview.pr ? <span className="text-muted-foreground"> #{a.preview.pr}</span> : null}
                </CardTitle>
              </CardHeader>
              <CardContent className="max-h-72 overflow-auto">
                <p className="eyebrow mb-2">Would post</p>
                <div className="rounded-md border border-border/40 bg-muted/50 p-3">
                  <Markdown>{a.preview.body ?? "(no preview)"}</Markdown>
                </div>
              </CardContent>
              <CardFooter className="mt-auto justify-between gap-2 border-t-[1.5px] border-border pt-(--card-spacing)">
                {a.status === "pending" ? (
                  canDecide ? (
                    <>
                      <Button variant="outline" size="sm" onClick={() => setPending({ a, decision: "reject" })}>
                        <X /> Reject
                      </Button>
                      <Button variant="neon" size="sm" bracket onClick={() => setPending({ a, decision: "approve" })}>
                        <Check /> Approve
                      </Button>
                    </>
                  ) : (
                    <p className="text-xs text-muted-foreground">Members and above can decide.</p>
                  )
                ) : (
                  <Badge variant={a.status === "approved" ? "success" : "destructive"}>
                    {a.status} by {a.decided_by ?? "unknown"}
                  </Badge>
                )}
              </CardFooter>
            </Card>
          </BlurFade>
        ))}
      </div>

      <AlertDialog open={!!pending} onOpenChange={(o) => !o && setPending(null)}>
        <AlertDialogContent className="border-[1.5px] border-border shadow-hard-lg">
          <AlertDialogHeader>
            <AlertDialogTitle className="display">
              {pending?.decision === "approve" ? "Approve and post?" : "Reject this action?"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {pending?.decision === "approve"
                ? "Argus resumes the run and posts the comment exactly once. Decisions are final."
                : "The run is cancelled and nothing is posted. Decisions are final."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor="reason" className="eyebrow">
              Reason (optional, recorded in the audit log)
            </Label>
            <Textarea id="reason" value={reason} onChange={(e) => setReason(e.target.value)} rows={2} maxLength={500} />
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={decide.isPending}
              onClick={(e) => {
                e.preventDefault()
                if (pending) decide.mutate({ id: pending.a.id, decision: pending.decision })
              }}
            >
              {pending?.decision === "approve" ? "Approve" : "Reject"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
