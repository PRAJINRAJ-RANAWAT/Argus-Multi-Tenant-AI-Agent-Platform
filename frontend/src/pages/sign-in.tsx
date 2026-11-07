import { useQuery } from "@tanstack/react-query"
import { LogIn } from "lucide-react"

import { BlurFade } from "@/components/motion/blur-fade"
import { BorderBeam } from "@/components/motion/border-beam"
import { ThemeToggle } from "@/components/theme-toggle"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { api } from "@/lib/api"

function GitHubMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" aria-hidden className={className} fill="currentColor">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
    </svg>
  )
}

const FEATURES = [
  ["01", "Reviews every pull request", "Bugs, security and missing tests, cited to the line."],
  ["02", "Answers questions about code", "Hybrid search over your repositories, every claim cited."],
  ["03", "Safe by construction", "Tenant isolation, approvals, guardrails and an audit log."],
]

export function SignInPage() {
  const config = useQuery({ queryKey: ["auth-config"], queryFn: api.authConfig })
  const next = encodeURIComponent(window.location.pathname + window.location.search)
  return (
    <div className="relative min-h-svh overflow-hidden bg-background">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 opacity-[0.35] [background-image:linear-gradient(var(--border)_1px,transparent_1px),linear-gradient(90deg,var(--border)_1px,transparent_1px)] [background-size:48px_48px] [mask-image:radial-gradient(ellipse_at_center,black_20%,transparent_70%)] dark:opacity-[0.12]"
      />
      <div className="absolute top-4 right-4">
        <ThemeToggle />
      </div>
      <main className="relative mx-auto grid min-h-svh max-w-6xl items-center gap-12 px-6 py-16 lg:grid-cols-[1.2fr_1fr]">
        <BlurFade>
          <p className="eyebrow mb-4">Argus · multi-tenant AI agents</p>
          <h1 className="display text-5xl leading-[0.9] sm:text-7xl">
            Reviews that
            <br />
            <span className="relative inline-block">
              <span className="relative z-10">never</span>
              <span aria-hidden className="absolute inset-x-0 bottom-1 z-0 h-4 bg-neon sm:h-6" />
            </span>{" "}
            double&nbsp;post.
          </h1>
          <p className="mt-6 max-w-lg text-base text-muted-foreground">
            A GitHub App that reviews pull requests and answers questions about your code, built to keep
            working through crashes, floods and prompt injections.
          </p>
          <ol className="mt-10 grid gap-4 sm:grid-cols-3">
            {FEATURES.map(([n, title, body]) => (
              <li key={n} className="border-t-[1.5px] border-border pt-3">
                <p className="num text-xs text-muted-foreground">{n}</p>
                <p className="mt-1 text-sm font-semibold">{title}</p>
                <p className="mt-1 text-xs text-muted-foreground">{body}</p>
              </li>
            ))}
          </ol>
        </BlurFade>
        <BlurFade delay={0.1}>
          <div className="panel relative p-6 shadow-hard-lg sm:p-8">
            <BorderBeam duration={7} />
            <h2 className="display text-xl">Sign in</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Use the GitHub account that has access to your organisation's Argus installation.
            </p>
            <div className="mt-6 space-y-3">
              {config.isPending && <Skeleton className="h-11 w-full" />}
              {config.data?.github && (
                <Button asChild size="lg" className="w-full">
                  <a href={`/auth/github/login?next=${next}`}>
                    <GitHubMark className="size-4" /> Continue with GitHub
                  </a>
                </Button>
              )}
              {config.data?.dev_login && (
                <Button asChild size="lg" variant="neon" className="w-full">
                  <a href={`/auth/dev-login?login=demo&next=${next}`}>
                    <LogIn /> Enter the local demo
                  </a>
                </Button>
              )}
              {config.data && !config.data.github && !config.data.dev_login && (
                <p className="rounded-md border-[1.5px] border-warning bg-warning/10 p-3 text-sm">
                  GitHub sign-in is not configured on this server. Set the GitHub App's OAuth client id and
                  secret (see docs/github-app.md).
                </p>
              )}
              {config.isError && (
                <p className="text-sm text-destructive">Cannot reach the Argus API. Is it running?</p>
              )}
            </div>
          </div>
        </BlurFade>
      </main>
    </div>
  )
}
