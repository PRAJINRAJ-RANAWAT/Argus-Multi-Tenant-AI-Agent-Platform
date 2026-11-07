import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Copy, KeyRound, Plus, Trash2, Users } from "lucide-react"
import { toast } from "sonner"

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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api, type ApiKey, type Role } from "@/lib/api"
import { timeAgo } from "@/lib/format"
import { useCan, useSession } from "@/lib/session"

const head = "font-mono text-[11px] uppercase tracking-wider"

function NewKeyDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const me = useSession()
  const [name, setName] = useState("")
  const [role, setRole] = useState<Role>("member")
  const [token, setToken] = useState<string | null>(null)
  const create = useMutation({
    mutationFn: () => api.createKey(name.trim(), role, null),
    onSuccess: (res) => {
      setToken(res.token)
      qc.invalidateQueries({ queryKey: ["keys"] })
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Could not create the key"),
  })
  const close = (o: boolean) => {
    if (!o) {
      setName("")
      setToken(null)
      setRole("member")
    }
    onOpenChange(o)
  }
  const roles: Role[] = (["viewer", "member", "admin"] as Role[]).filter(
    (r) => me.tenant.role === "owner" || me.tenant.role === "admin" || r !== "admin",
  )
  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="border-[1.5px] border-border shadow-hard-lg">
        <DialogHeader>
          <DialogTitle className="display">{token ? "Copy your key now" : "New API key"}</DialogTitle>
          <DialogDescription>
            {token
              ? "This is the only time the key is shown. Argus stores only its hash."
              : "Keys act within this organisation with a role no higher than yours."}
          </DialogDescription>
        </DialogHeader>
        {token ? (
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded-md border-[1.5px] border-border bg-muted px-3 py-2 font-mono text-xs">
              {token}
            </code>
            <Button
              size="icon"
              variant="outline"
              aria-label="Copy key"
              onClick={() => {
                navigator.clipboard.writeText(token).then(
                  () => toast.success("Copied"),
                  () => toast.error("Copy failed; select the key and copy it manually"),
                )
              }}
            >
              <Copy />
            </Button>
          </div>
        ) : (
          <form
            id="new-key"
            className="space-y-4"
            onSubmit={(e) => {
              e.preventDefault()
              if (name.trim()) create.mutate()
            }}
          >
            <div className="space-y-1.5">
              <Label htmlFor="key-name" className="eyebrow">
                Name
              </Label>
              <Input id="key-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="ci-pipeline" maxLength={100} autoFocus />
            </div>
            <div className="space-y-1.5">
              <Label className="eyebrow">Role</Label>
              <Select value={role} onValueChange={(v) => setRole(v as Role)}>
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {roles.map((r) => (
                    <SelectItem key={r} value={r}>
                      {r}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </form>
        )}
        <DialogFooter>
          {token ? (
            <Button onClick={() => close(false)}>Done</Button>
          ) : (
            <Button type="submit" form="new-key" variant="neon" bracket disabled={!name.trim() || create.isPending}>
              Create key
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function KeysTab() {
  const qc = useQueryClient()
  const keys = useQuery({ queryKey: ["keys"], queryFn: api.keys })
  const [creating, setCreating] = useState(false)
  const [revoking, setRevoking] = useState<ApiKey | null>(null)
  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeKey(id),
    onSuccess: () => {
      toast.success("Key revoked; it stops working immediately")
      qc.invalidateQueries({ queryKey: ["keys"] })
      setRevoking(null)
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Revoke failed"),
  })
  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Button variant="neon" bracket onClick={() => setCreating(true)}>
          <Plus /> New key
        </Button>
      </div>
      {keys.isError && <ErrorState error={keys.error} onRetry={() => keys.refetch()} />}
      <div className="panel overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow className="border-b-[1.5px] border-border">
              <TableHead className={head}>Name</TableHead>
              <TableHead className={head}>Key</TableHead>
              <TableHead className={head}>Role</TableHead>
              <TableHead className={`${head} hidden md:table-cell`}>Last used</TableHead>
              <TableHead className={head}>Status</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {keys.isPending && (
              <TableRow>
                <TableCell colSpan={6}>
                  <Skeleton className="h-6 w-full" />
                </TableCell>
              </TableRow>
            )}
            {keys.data?.keys.map((k) => (
              <TableRow key={k.id} className="border-border/30">
                <TableCell className="font-medium">{k.name}</TableCell>
                <TableCell className="font-mono text-xs">argus_{k.prefix}_…</TableCell>
                <TableCell>
                  <span className="tag">{k.role}</span>
                </TableCell>
                <TableCell className="num hidden text-xs text-muted-foreground md:table-cell">
                  {k.last_used_at ? timeAgo(k.last_used_at) : "never"}
                </TableCell>
                <TableCell>
                  <Badge variant={k.revoked ? "outline" : "success"}>{k.revoked ? "revoked" : "active"}</Badge>
                </TableCell>
                <TableCell className="text-right">
                  {!k.revoked && (
                    <Button size="icon-sm" variant="ghost" aria-label={`Revoke ${k.name}`} onClick={() => setRevoking(k)}>
                      <Trash2 />
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {keys.data?.keys.length === 0 && (
          <div className="p-4">
            <EmptyState icon={KeyRound} title="No API keys">
              Keys let CI or scripts ask questions and read runs without a browser session.
            </EmptyState>
          </div>
        )}
      </div>
      <NewKeyDialog open={creating} onOpenChange={setCreating} />
      <AlertDialog open={!!revoking} onOpenChange={(o) => !o && setRevoking(null)}>
        <AlertDialogContent className="border-[1.5px] border-border shadow-hard-lg">
          <AlertDialogHeader>
            <AlertDialogTitle className="display">Revoke “{revoking?.name}”?</AlertDialogTitle>
            <AlertDialogDescription>
              Anything using this key gets 401 from now on. This cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                if (revoking) revoke.mutate(revoking.id)
              }}
              className="bg-destructive text-white"
            >
              Revoke
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

function MembersTab() {
  const qc = useQueryClient()
  const isOwner = useCan("owner")
  const me = useSession()
  const members = useQuery({ queryKey: ["members"], queryFn: api.members })
  const setRole = useMutation({
    mutationFn: ({ id, role }: { id: string; role: Role }) => api.setRole(id, role),
    onSuccess: () => {
      toast.success("Role updated")
      qc.invalidateQueries({ queryKey: ["members"] })
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Could not change the role"),
  })
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        Members come from GitHub: everyone who can access the App installation. Effective access to a private
        repository also needs the person's own GitHub permission on it.
      </p>
      {members.isError && <ErrorState error={members.error} onRetry={() => members.refetch()} />}
      <div className="panel overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow className="border-b-[1.5px] border-border">
              <TableHead className={head}>Member</TableHead>
              <TableHead className={head}>Role</TableHead>
              <TableHead className={`${head} hidden sm:table-cell`}>Source</TableHead>
              <TableHead className={`${head} hidden md:table-cell`}>Last sign-in</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {members.isPending && (
              <TableRow>
                <TableCell colSpan={4}>
                  <Skeleton className="h-6 w-full" />
                </TableCell>
              </TableRow>
            )}
            {members.data?.members.map((m) => (
              <TableRow key={m.user_id} className="border-border/30">
                <TableCell>
                  <p className="font-medium">
                    {m.github_login}
                    {m.github_login === me.actor.github_login && <span className="text-muted-foreground"> (you)</span>}
                  </p>
                  {m.name && m.name !== m.github_login && <p className="text-xs text-muted-foreground">{m.name}</p>}
                </TableCell>
                <TableCell>
                  {isOwner ? (
                    <Select value={m.role} onValueChange={(role) => setRole.mutate({ id: m.user_id, role: role as Role })}>
                      <SelectTrigger className="h-8 w-28 text-xs" aria-label={`Role of ${m.github_login}`}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {(["viewer", "member", "admin", "owner"] as Role[]).map((r) => (
                          <SelectItem key={r} value={r}>
                            {r}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  ) : (
                    <span className="tag">{m.role}</span>
                  )}
                </TableCell>
                <TableCell className="hidden sm:table-cell">
                  <Badge variant="outline">{m.source}</Badge>
                </TableCell>
                <TableCell className="num hidden text-xs text-muted-foreground md:table-cell">
                  {m.last_login_at ? timeAgo(m.last_login_at) : "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {members.data?.members.length === 0 && (
          <div className="p-4">
            <EmptyState icon={Users} title="No members" />
          </div>
        )}
      </div>
    </div>
  )
}

export function AccessPage() {
  return (
    <>
      <PageHeader
        eyebrow="Access"
        title="Keys and members"
        description="Who and what can act in this organisation. Every change is written to the audit log."
      />
      <Tabs defaultValue="keys">
        <TabsList className="mb-4">
          <TabsTrigger value="keys">
            <KeyRound /> API keys
          </TabsTrigger>
          <TabsTrigger value="members">
            <Users /> Members
          </TabsTrigger>
        </TabsList>
        <TabsContent value="keys">
          <KeysTab />
        </TabsContent>
        <TabsContent value="members">
          <MembersTab />
        </TabsContent>
      </Tabs>
    </>
  )
}
