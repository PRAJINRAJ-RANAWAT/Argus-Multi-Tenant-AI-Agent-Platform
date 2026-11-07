import {
  Activity,
  BarChart3,
  CheckCheck,
  KeyRound,
  LayoutDashboard,
  MessageSquareCode,
  ScrollText,
  Settings,
  type LucideIcon,
} from "lucide-react"

import type { Role } from "@/lib/api"

export interface NavItem {
  to: string
  label: string
  icon: LucideIcon
  min: Role
  keywords?: string
}

export const NAV: { group: string; items: NavItem[] }[] = [
  {
    group: "Work",
    items: [
      { to: "/", label: "Overview", icon: LayoutDashboard, min: "viewer", keywords: "home kpi" },
      { to: "/runs", label: "Runs", icon: Activity, min: "viewer", keywords: "reviews history" },
      { to: "/ask", label: "Ask the code", icon: MessageSquareCode, min: "member", keywords: "qa question search" },
      { to: "/approvals", label: "Approvals", icon: CheckCheck, min: "viewer", keywords: "pending decide" },
    ],
  },
  {
    group: "Manage",
    items: [
      { to: "/usage", label: "Usage & budget", icon: BarChart3, min: "viewer", keywords: "tokens cost llm" },
      { to: "/access", label: "Access", icon: KeyRound, min: "admin", keywords: "api keys members roles" },
      { to: "/audit", label: "Audit log", icon: ScrollText, min: "admin", keywords: "security history" },
      { to: "/settings", label: "Settings", icon: Settings, min: "viewer", keywords: "approval theme" },
    ],
  },
]
