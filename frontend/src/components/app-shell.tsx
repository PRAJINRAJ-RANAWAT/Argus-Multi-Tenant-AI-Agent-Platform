import { useEffect, useState } from "react"
import { NavLink, Outlet, useLocation, useNavigate } from "react-router"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Check, ChevronsUpDown, LogOut, Search } from "lucide-react"

import { CommandMenu } from "@/components/command-menu"
import { ThemeToggle } from "@/components/theme-toggle"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useRunStream, type StreamState } from "@/hooks/use-run-stream"
import { api, setTenantCookie } from "@/lib/api"
import { NAV } from "@/lib/nav"
import { useCan, useDemo, useSession } from "@/lib/session"
import { cn } from "@/lib/utils"

function Logo() {
  return (
    <div className="flex items-center gap-2 px-1 py-1">
      <div className="grid size-8 shrink-0 place-items-center rounded-md border-[1.5px] border-border bg-ink shadow-hard-sm">
        <span className="size-3.5 rounded-full border-[3px] border-neon" />
      </div>
      <div className="leading-none group-data-[collapsible=icon]:hidden">
        <p className="display text-base">Argus</p>
        <p className="eyebrow mt-0.5 text-[9px]">AI agent platform</p>
      </div>
    </div>
  )
}

function NavItems() {
  const { setOpenMobile } = useSidebar()
  const { pathname } = useLocation()
  const isActive = (to: string) =>
    to === "/" ? pathname === "/" : pathname === to || pathname.startsWith(`${to}/`)
  const pending = useQuery({
    queryKey: ["approvals", "pending"],
    queryFn: () => api.approvals("pending"),
    refetchInterval: 60_000,
  })
  const pendingCount = pending.data?.approvals.length ?? 0
  const canMember = useCan("member")
  const canAdmin = useCan("admin")
  const demo = useDemo()
  const allowed = (min: string, to?: string) =>
    min === "viewer" ||
    (min === "member" && (canMember || (demo && to === "/ask"))) ||
    (min === "admin" && canAdmin)

  return (
    <>
      {NAV.map((section) => (
        <SidebarGroup key={section.group}>
          <SidebarGroupLabel className="eyebrow text-[10px]">{section.group}</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {section.items.filter((i) => allowed(i.min, i.to)).map((item) => (
                <SidebarMenuItem key={item.to}>
                  <SidebarMenuButton
                    asChild
                    isActive={isActive(item.to)}
                    tooltip={item.label}
                    className={cn(
                      "relative font-medium",
                      isActive(item.to) &&
                        "before:absolute before:inset-y-1.5 before:left-0 before:w-[3px] before:rounded-full before:bg-neon",
                    )}
                  >
                    <NavLink to={item.to} end={item.to === "/"} onClick={() => setOpenMobile(false)}>
                      <item.icon />
                      <span>{item.label}</span>
                    </NavLink>
                  </SidebarMenuButton>
                  {item.to === "/approvals" && pendingCount > 0 && (
                    <SidebarMenuBadge className="num rounded-sm bg-warning/15 text-warning">
                      {pendingCount}
                    </SidebarMenuBadge>
                  )}
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      ))}
    </>
  )
}

function UserMenu() {
  const me = useSession()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const name = me.actor.github_login ?? "API key"
  const switchTenant = (login: string) => {
    setTenantCookie(login)
    qc.clear()
    navigate("/")
  }
  const signOut = async () => {
    await api.logout().catch(() => undefined)
    qc.clear()
    window.location.assign("/app/")
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <SidebarMenuButton size="lg" className="data-[state=open]:bg-sidebar-accent">
          <Avatar className="size-8 rounded-md border-[1.5px] border-border">
            <AvatarFallback className="rounded-md bg-neon font-mono text-xs font-bold text-neon-foreground">
              {name.slice(0, 2).toUpperCase()}
            </AvatarFallback>
          </Avatar>
          <div className="grid flex-1 text-left leading-tight">
            <span className="truncate text-sm font-semibold">{name}</span>
            <span className="truncate font-mono text-[11px] text-muted-foreground">
              {me.tenant.login} · {me.tenant.role}
            </span>
          </div>
          <ChevronsUpDown className="ml-auto size-4" />
        </SidebarMenuButton>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="start" className="w-60">
        {me.tenants.length > 1 && (
          <>
            <DropdownMenuLabel className="eyebrow">Organisation</DropdownMenuLabel>
            {me.tenants.map((t) => (
              <DropdownMenuItem key={t.login} onSelect={() => switchTenant(t.login)}>
                <span className="flex-1 truncate">{t.login}</span>
                <span className="font-mono text-[11px] text-muted-foreground">{t.role}</span>
                {t.login === me.tenant.login && <Check className="size-4" />}
              </DropdownMenuItem>
            ))}
            <DropdownMenuSeparator />
          </>
        )}
        <DropdownMenuItem onSelect={signOut}>
          <LogOut /> Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function LiveIndicator({ state }: { state: StreamState }) {
  const label = state === "live" ? "Live" : state === "connecting" ? "Connecting" : "Reconnecting"
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className="hidden items-center gap-1.5 font-mono text-[11px] uppercase tracking-wider text-muted-foreground sm:inline-flex"
          role="status"
        >
          <span
            className={cn(
              "size-2 rounded-full",
              state === "live" ? "bg-neon shadow-[0_0_8px_var(--neon)]" : "bg-warning",
              state === "live" && "animate-pulse motion-reduce:animate-none",
            )}
          />
          {label}
        </span>
      </TooltipTrigger>
      <TooltipContent>
        {state === "live" ? "Runs update in real time" : "Live updates paused; retrying"}
      </TooltipContent>
    </Tooltip>
  )
}

function DemoBanner() {
  return (
    <div
      role="note"
      className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1 border-b-[1.5px] border-border bg-neon px-4 py-1.5 text-center text-neon-foreground"
    >
      <span className="font-mono text-[11px] font-bold uppercase tracking-[0.18em]">
        Demo mode: read-only
      </span>
      <span className="text-xs">
        Sample data for a fictional team. Explore freely; ask the code a question.
      </span>
      <a href="/" className="text-xs font-semibold underline underline-offset-2">
        Back to the site
      </a>
    </div>
  )
}

const TITLES: Record<string, string> = Object.fromEntries(
  NAV.flatMap((s) => s.items).map((i) => [i.to, i.label]),
)

export function AppShell() {
  const location = useLocation()
  const me = useSession()
  const stream = useRunStream(true)
  const [commandOpen, setCommandOpen] = useState(false)
  const section = "/" + (location.pathname.split("/")[1] ?? "")
  const title = TITLES[section] ?? "Argus"

  useEffect(() => {
    document.title = `${title} · Argus`
  }, [title])

  return (
    <SidebarProvider>
      <Sidebar collapsible="icon" className="border-r-[1.5px]">
        <SidebarHeader>
          <Logo />
        </SidebarHeader>
        <SidebarContent>
          <NavItems />
        </SidebarContent>
        <SidebarFooter>
          <SidebarMenu>
            <SidebarMenuItem>
              <UserMenu />
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarFooter>
        <SidebarRail />
      </Sidebar>
      <SidebarInset className="min-w-0">
        {me.demo && <DemoBanner />}
        <header className="sticky top-0 z-20 flex h-14 items-center gap-2 border-b-[1.5px] border-border bg-background/85 px-3 backdrop-blur sm:px-4">
          <SidebarTrigger className="-ml-1" />
          <Breadcrumb className="min-w-0">
            <BreadcrumbList>
              <BreadcrumbItem className="hidden font-mono text-xs uppercase sm:block">
                {me.tenant.login}
              </BreadcrumbItem>
              <BreadcrumbSeparator className="hidden sm:block" />
              <BreadcrumbItem>
                <BreadcrumbPage className="truncate font-semibold">{title}</BreadcrumbPage>
              </BreadcrumbItem>
            </BreadcrumbList>
          </Breadcrumb>
          <div className="ml-auto flex items-center gap-2">
            <LiveIndicator state={stream} />
            <Button
              variant="outline"
              size="sm"
              className="w-9 justify-start px-2 text-muted-foreground normal-case tracking-normal sm:w-56"
              onClick={() => setCommandOpen(true)}
              aria-label="Search and commands"
            >
              <Search />
              <span className="hidden flex-1 text-left font-normal sm:inline">Search or jump to…</span>
              <kbd className="pointer-events-none hidden rounded-sm border border-border/50 px-1 font-mono text-[10px] sm:inline">
                ⌘K
              </kbd>
            </Button>
            <ThemeToggle />
          </div>
        </header>
        <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6 lg:px-8">
          <Outlet />
        </main>
      </SidebarInset>
      <CommandMenu open={commandOpen} onOpenChange={setCommandOpen} />
    </SidebarProvider>
  )
}
