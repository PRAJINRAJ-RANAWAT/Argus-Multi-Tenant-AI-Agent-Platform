import { useEffect, useState } from "react"
import { useNavigate } from "react-router"
import { useQuery } from "@tanstack/react-query"
import { Laptop, Moon, Sun } from "lucide-react"

import { StatusBadge } from "@/components/status-badge"
import { useTheme } from "@/components/theme-provider"
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command"
import { api } from "@/lib/api"
import { AGENT_LABEL } from "@/lib/format"
import { NAV } from "@/lib/nav"
import { useCan, useDemo } from "@/lib/session"

/** ⌘K / Ctrl+K: jump to a page, search runs by repository or question, switch theme. */
export function CommandMenu({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const navigate = useNavigate()
  const { setTheme } = useTheme()
  const [query, setQuery] = useState("")
  const [debounced, setDebounced] = useState("")
  const canMember = useCan("member")
  const canAdmin = useCan("admin")
  const demo = useDemo()

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault()
        onOpenChange(!open)
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open, onOpenChange])

  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(query.trim()), 200)
    return () => window.clearTimeout(t)
  }, [query])

  const runs = useQuery({
    queryKey: ["runs", "search", debounced],
    queryFn: () => api.runs({ q: debounced, limit: 6 }),
    enabled: open && debounced.length >= 2,
  })

  const go = (to: string) => {
    onOpenChange(false)
    setQuery("")
    navigate(to)
  }
  const allowed = (min: string, to?: string) =>
    min === "viewer" ||
    (min === "member" && (canMember || (demo && to === "/ask"))) ||
    (min === "admin" && canAdmin)

  return (
    <CommandDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Search and commands"
      className="rounded-md! border-[1.5px] border-border shadow-hard-lg"
    >
      <Command className="bg-popover">
      <CommandInput placeholder="Jump to a page or search runs…" value={query} onValueChange={setQuery} />
      <CommandList>
        <CommandEmpty>{runs.isFetching ? "Searching…" : "No results."}</CommandEmpty>
        {debounced.length >= 2 && (runs.data?.runs.length ?? 0) > 0 && (
          <CommandGroup heading="Runs">
            {runs.data!.runs.map((r) => (
              <CommandItem
                key={r.id}
                value={`run ${r.id} ${r.repo ?? ""} ${debounced}`}
                onSelect={() => go(`/runs/${r.id}`)}
              >
                <span className="truncate">
                  {r.repo ?? "—"}
                  {r.pr_number ? ` #${r.pr_number}` : ""}
                </span>
                <span className="ml-auto flex items-center gap-2">
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {AGENT_LABEL[r.agent_type] ?? r.agent_type}
                  </span>
                  <StatusBadge status={r.status} />
                </span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}
        {NAV.map((section) => (
          <CommandGroup key={section.group} heading={section.group}>
            {section.items.filter((i) => allowed(i.min, i.to)).map((item) => (
              <CommandItem
                key={item.to}
                value={`${item.label} ${item.keywords ?? ""}`}
                onSelect={() => go(item.to)}
              >
                <item.icon />
                {item.label}
              </CommandItem>
            ))}
          </CommandGroup>
        ))}
        <CommandSeparator />
        <CommandGroup heading="Theme">
          <CommandItem value="theme light" onSelect={() => setTheme("light")}>
            <Sun /> Light
          </CommandItem>
          <CommandItem value="theme dark" onSelect={() => setTheme("dark")}>
            <Moon /> Dark
          </CommandItem>
          <CommandItem value="theme system" onSelect={() => setTheme("system")}>
            <Laptop /> System
          </CommandItem>
        </CommandGroup>
      </CommandList>
      </Command>
    </CommandDialog>
  )
}
