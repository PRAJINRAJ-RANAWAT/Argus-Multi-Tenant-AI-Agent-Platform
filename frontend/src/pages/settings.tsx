import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Laptop, Moon, Sun } from "lucide-react"
import { toast } from "sonner"

import { PageHeader } from "@/components/page-header"
import { ErrorState } from "@/components/states"
import { useTheme, type Theme } from "@/components/theme-provider"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { api } from "@/lib/api"
import { useCan } from "@/lib/session"
import { cn } from "@/lib/utils"

const TOOL_TEXT: Record<string, { title: string; body: string }> = {
  post_review_comment: {
    title: "Review comments",
    body: "A member approves each PR review before Argus posts it.",
  },
  open_issue: {
    title: "Opening issues",
    body: "A member approves before Argus opens an issue.",
  },
}

const THEMES: { value: Theme; label: string; icon: typeof Sun }[] = [
  { value: "light", label: "Light", icon: Sun },
  { value: "dark", label: "Dark", icon: Moon },
  { value: "system", label: "System", icon: Laptop },
]

export function SettingsPage() {
  const qc = useQueryClient()
  const canManage = useCan("admin")
  const { theme, setTheme } = useTheme()
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings })
  const save = useMutation({
    mutationFn: (tools: string[]) => api.setSettings(tools),
    onMutate: async (tools) => {
      await qc.cancelQueries({ queryKey: ["settings"] })
      const prev = qc.getQueryData(["settings"])
      qc.setQueryData(["settings"], (old: typeof settings.data) =>
        old ? { ...old, require_approval_for: tools } : old,
      )
      return { prev }
    },
    onError: (e, _, ctx) => {
      qc.setQueryData(["settings"], ctx?.prev)
      toast.error(e instanceof Error ? e.message : "Could not save")
    },
    onSuccess: () => toast.success("Approval settings saved"),
  })
  const required = new Set(settings.data?.require_approval_for ?? [])
  const toggle = (tool: string, on: boolean) => {
    const next = new Set(required)
    if (on) next.add(tool)
    else next.delete(tool)
    save.mutate([...next].sort())
  }

  return (
    <>
      <PageHeader eyebrow="Settings" title="Preferences" />
      <div className="grid max-w-3xl gap-6">
        <Card>
          <CardHeader>
            <CardTitle className="display text-sm">Human approval</CardTitle>
            <CardDescription>
              {canManage
                ? "Choose which write actions wait for a person. Runs waiting for approval hold no worker."
                : "Only admins can change these."}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {settings.isError && <ErrorState error={settings.error} onRetry={() => settings.refetch()} />}
            {settings.isPending && <Skeleton className="h-24" />}
            {settings.data?.write_tools.map((tool) => (
              <div key={tool} className="flex items-start justify-between gap-4 rounded-md border border-border/40 p-3">
                <div>
                  <Label htmlFor={`approve-${tool}`} className="font-semibold">
                    {TOOL_TEXT[tool]?.title ?? tool}
                  </Label>
                  <p className="text-sm text-muted-foreground">{TOOL_TEXT[tool]?.body}</p>
                </div>
                <Switch
                  id={`approve-${tool}`}
                  checked={required.has(tool)}
                  disabled={!canManage || save.isPending}
                  onCheckedChange={(on) => toggle(tool, on)}
                />
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="display text-sm">Appearance</CardTitle>
            <CardDescription>Saved in this browser. System follows your OS setting.</CardDescription>
          </CardHeader>
          <CardContent>
            <div role="radiogroup" aria-label="Theme" className="grid grid-cols-3 gap-3">
              {THEMES.map((t) => (
                <button
                  key={t.value}
                  type="button"
                  role="radio"
                  aria-checked={theme === t.value}
                  onClick={() => setTheme(t.value)}
                  className={cn(
                    "panel panel-interactive flex flex-col items-center gap-2 p-4 text-sm font-medium",
                    theme === t.value && "bg-neon text-neon-foreground",
                  )}
                >
                  <t.icon className="size-5" />
                  {t.label}
                </button>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>
    </>
  )
}
