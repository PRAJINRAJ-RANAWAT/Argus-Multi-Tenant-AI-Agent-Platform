# Argus web app

React 19 + TypeScript + Vite, Tailwind CSS v4, shadcn/ui (Radix primitives), TanStack Query, Recharts (shadcn charts), Motion. Served by the API at `/app`.

```bash
npm install
npm run dev     # http://localhost:5173/app/, proxies /api and /auth to the API on :8000
npm run lint    # oxlint
npm run build   # type-check, then build into ../src/argus/api/web (served at /app)
```

Sign in locally with the demo stack: http://localhost:5173/auth/dev-login?login=demo&next=/app/

- `src/index.css`: design tokens (light and dark), hard shadows, typography
- `src/components/ui`: shadcn components, adapted to the design system
- `src/components/motion`: number ticker, blur fade, border beam, spotlight card (Magic UI / React Bits patterns, written against Motion)
- `src/lib/api.ts`: typed API client; `src/hooks/use-run-stream.ts`: live updates over SSE
