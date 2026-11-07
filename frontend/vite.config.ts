import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// The SPA is served by the API at /app in production. In development Vite proxies
// the API so the session cookie stays same-origin.
const api = process.env.ARGUS_API ?? "http://localhost:8000"

export default defineConfig({
  base: "/app/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  // Built into the Python package so the API serves it at /app (see docker/Dockerfile).
  build: {
    outDir: path.resolve(__dirname, "../src/argus/api/web"),
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // Split rarely-changing vendor code so app updates do not bust its cache.
        manualChunks(id: string) {
          if (!id.includes("node_modules")) return undefined
          if (/recharts|d3-|victory/.test(id)) return "charts"
          if (/react-dom|react-router|[\\/]react[\\/]|scheduler/.test(id)) return "react"
          if (/radix-ui|@radix-ui|cmdk|motion|sonner/.test(id)) return "ui"
          return undefined
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: api, changeOrigin: false },
      "/auth": { target: api, changeOrigin: false },
    },
  },
})
