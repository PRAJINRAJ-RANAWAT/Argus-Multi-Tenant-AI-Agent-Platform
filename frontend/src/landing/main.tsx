import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import "../index.css"
import { ThemeProvider } from "@/components/theme-provider"

import { Landing } from "./landing"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider defaultTheme="dark">
      <Landing />
    </ThemeProvider>
  </StrictMode>,
)
