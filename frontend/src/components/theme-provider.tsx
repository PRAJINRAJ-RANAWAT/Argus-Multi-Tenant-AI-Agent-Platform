import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react"

export type Theme = "light" | "dark" | "system"
type Resolved = "light" | "dark"

export const THEME_KEY = "argus-theme"

type ThemeContextValue = {
  theme: Theme
  resolved: Resolved
  setTheme: (theme: Theme) => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

function readStored(fallback: Theme): Theme {
  try {
    const v = localStorage.getItem(THEME_KEY)
    return v === "light" || v === "dark" || v === "system" ? v : fallback
  } catch {
    return fallback // storage blocked (private mode, sandbox): use the page default
  }
}

const systemDark = () => window.matchMedia("(prefers-color-scheme: dark)").matches

function apply(resolved: Resolved, animate: boolean) {
  const root = document.documentElement
  if (animate) {
    root.classList.add("theme-transition")
    window.setTimeout(() => root.classList.remove("theme-transition"), 250)
  }
  root.classList.toggle("dark", resolved === "dark")
}

/** Light, dark or system theme, persisted per browser. The inline script in
 *  index.html applies the same choice before first paint, so there is no flash. */
export function ThemeProvider({
  children,
  defaultTheme = "system",
}: {
  children: React.ReactNode
  /** Used until the visitor picks a theme (the landing page is dark-first). */
  defaultTheme?: Theme
}) {
  const [theme, setThemeState] = useState<Theme>(() => readStored(defaultTheme))
  const [system, setSystem] = useState<Resolved>(() => (systemDark() ? "dark" : "light"))
  const resolved: Resolved = theme === "system" ? system : theme

  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)")
    const onChange = () => setSystem(mq.matches ? "dark" : "light")
    mq.addEventListener("change", onChange)
    return () => mq.removeEventListener("change", onChange)
  }, [])

  useEffect(() => apply(resolved, false), [resolved])

  const setTheme = useCallback((next: Theme) => {
    try {
      localStorage.setItem(THEME_KEY, next)
    } catch {
      /* non-persistent is fine */
    }
    const nextResolved = next === "system" ? (systemDark() ? "dark" : "light") : next
    apply(nextResolved, true)
    setThemeState(next)
  }, [])

  const value = useMemo(() => ({ theme, resolved, setTheme }), [theme, resolved, setTheme])
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme() {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error("useTheme must be used inside ThemeProvider")
  return ctx
}
