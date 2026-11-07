import { createContext, useContext } from "react"

import type { Me, Role } from "@/lib/api"

const RANK: Record<Role, number> = { viewer: 0, member: 1, admin: 2, owner: 3 }

export const SessionContext = createContext<Me | null>(null)

export function useSession(): Me {
  const me = useContext(SessionContext)
  if (!me) throw new Error("useSession outside an authenticated route")
  return me
}

/** Mirrors the API's role checks so the UI hides actions the API would refuse. The
 *  API remains the authority: hidden or not, it enforces every permission itself. */
export function useCan(min: Role): boolean {
  return RANK[useSession().tenant.role] >= RANK[min]
}

/** Public demo visitor: read-only everywhere, except asking the codebase a question. */
export function useDemo(): boolean {
  return !!useSession().demo
}
