import { BrowserRouter, Link, Route, Routes } from "react-router"
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query"
import { Compass, Loader2, ShieldOff } from "lucide-react"

import { AppShell } from "@/components/app-shell"
import { EmptyState, ErrorState } from "@/components/states"
import { ThemeProvider } from "@/components/theme-provider"
import { Button } from "@/components/ui/button"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { ApiError, api, type Role } from "@/lib/api"
import { SessionContext, useCan, useDemo } from "@/lib/session"
import { AccessPage } from "@/pages/access"
import { ApprovalsPage } from "@/pages/approvals"
import { AskPage } from "@/pages/ask"
import { AuditPage } from "@/pages/audit"
import { OverviewPage } from "@/pages/overview"
import { RunsPage } from "@/pages/runs"
import { SettingsPage } from "@/pages/settings"
import { SignInPage } from "@/pages/sign-in"
import { UsagePage } from "@/pages/usage"

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: true,
      retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
    },
  },
})

function RequireRole({
  min,
  children,
  demoAllowed = false,
}: {
  min: Role
  children: React.ReactNode
  demoAllowed?: boolean
}) {
  const demo = useDemo()
  if (!useCan(min) && !(demoAllowed && demo)) {
    return (
      <EmptyState icon={ShieldOff} title="Not available for your role">
        This page needs the {min} role in this organisation.
      </EmptyState>
    )
  }
  return <>{children}</>
}

function NotFound() {
  return (
    <EmptyState
      icon={Compass}
      title="Page not found"
      action={
        <Button asChild variant="outline">
          <Link to="/">Back to overview</Link>
        </Button>
      }
    />
  )
}

function Authenticated() {
  const me = useQuery({ queryKey: ["me"], queryFn: api.me, retry: false, staleTime: 60_000 })
  if (me.isPending) {
    return (
      <div className="grid min-h-svh place-items-center" role="status" aria-label="Loading">
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    )
  }
  if (me.isError) {
    if (me.error instanceof ApiError && me.error.status === 401) return <SignInPage />
    return (
      <div className="mx-auto max-w-lg p-6 pt-24">
        <ErrorState error={me.error} onRetry={() => me.refetch()} />
      </div>
    )
  }
  return (
    <SessionContext.Provider value={me.data}>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<OverviewPage />} />
          <Route path="runs" element={<RunsPage />} />
          <Route path="runs/:runId" element={<RunsPage />} />
          <Route
            path="ask"
            element={
              <RequireRole min="member" demoAllowed>
                <AskPage />
              </RequireRole>
            }
          />
          <Route path="approvals" element={<ApprovalsPage />} />
          <Route path="usage" element={<UsagePage />} />
          <Route
            path="access"
            element={
              <RequireRole min="admin">
                <AccessPage />
              </RequireRole>
            }
          />
          <Route
            path="audit"
            element={
              <RequireRole min="admin">
                <AuditPage />
              </RequireRole>
            }
          />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </SessionContext.Provider>
  )
}

export default function App() {
  return (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider delayDuration={200}>
          <BrowserRouter basename="/app">
            <Authenticated />
          </BrowserRouter>
          <Toaster position="bottom-right" />
        </TooltipProvider>
      </QueryClientProvider>
    </ThemeProvider>
  )
}
