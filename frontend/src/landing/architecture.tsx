// The architecture diagram from docs/SPEC.md as SVG. Nodes and edges carry the groups
// the "How it works" callouts highlight; edges draw in on scroll and a packet travels
// the main path of a PR review.

export type Group = "queue" | "durable" | "gateway" | "tenancy" | "evals"

interface NodeSpec {
  id: string
  x: number
  y: number
  w: number
  h: number
  title: string
  lines: string[]
  groups: Group[]
  external?: boolean
}

const NODES: NodeSpec[] = [
  { id: "github", x: 40, y: 16, w: 200, h: 54, title: "GitHub", lines: ["PR and push webhooks"], groups: ["queue"], external: true },
  { id: "slack", x: 380, y: 16, w: 200, h: 54, title: "Slack", lines: ["mentions, approvals"], groups: [], external: true },
  { id: "users", x: 720, y: 16, w: 200, h: 54, title: "Dashboard users", lines: ["browser, GitHub login"], groups: ["tenancy"], external: true },
  { id: "ingest", x: 40, y: 122, w: 340, h: 88, title: "Ingest service", lines: ["verify signature, dedupe delivery", "event + outbox row in one tx, 202"], groups: ["queue"] },
  { id: "api", x: 580, y: 122, w: 340, h: 88, title: "API service", lines: ["GitHub OAuth, roles, approvals", "live run updates over SSE"], groups: ["tenancy"] },
  { id: "postgres", x: 40, y: 266, w: 340, h: 88, title: "Postgres + pgvector", lines: ["runs, steps, outbox, audit log", "row-level security, code vectors"], groups: ["queue", "durable", "tenancy"] },
  { id: "redis", x: 580, y: 266, w: 340, h: 88, title: "Redis Streams", lines: ["a lane per tenant per job type, DLQ", "locks, rate limits, cache"], groups: ["queue"] },
  { id: "agents", x: 40, y: 410, w: 270, h: 88, title: "Agent workers", lines: ["durable runs with leases", "checkpoint every step"], groups: ["durable"] },
  { id: "indexer", x: 345, y: 410, w: 270, h: 88, title: "Indexer workers", lines: ["tree-sitter chunking", "embed changed code only"], groups: [] },
  { id: "evals", x: 650, y: 410, w: 270, h: 88, title: "Eval runner", lines: ["golden sets on every change", "blocks merges on regressions"], groups: ["evals"] },
  { id: "gateway", x: 40, y: 554, w: 420, h: 76, title: "LLM gateway", lines: ["routing, caching, token buckets, budgets, breaker"], groups: ["gateway"] },
  { id: "mcp", x: 500, y: 554, w: 420, h: 76, title: "MCP tool servers", lines: ["argus-code, argus-github, allowlists, approvals"], groups: [] },
  { id: "models", x: 40, y: 676, w: 420, h: 48, title: "Model providers", lines: [], groups: ["gateway"], external: true },
  { id: "apis", x: 500, y: 676, w: 420, h: 48, title: "GitHub and Slack APIs", lines: [], groups: [], external: true },
]

interface EdgeSpec {
  d: string
  groups: Group[]
  label?: { x: number; y: number; text: string }
}

const EDGES: EdgeSpec[] = [
  { d: "M140 70 V122", groups: ["queue"] },
  { d: "M480 70 C480 98 330 96 330 122", groups: [] },
  { d: "M820 70 V122", groups: ["tenancy"] },
  { d: "M210 210 V266", groups: ["queue", "durable"], label: { x: 222, y: 243, text: "event + outbox row" } },
  { d: "M380 310 H580", groups: ["queue"], label: { x: 418, y: 300, text: "outbox relay" } },
  { d: "M640 210 C640 240 330 236 330 266", groups: ["tenancy"] },
  { d: "M820 210 V266", groups: ["tenancy"] },
  { d: "M750 354 C750 384 175 380 175 410", groups: ["queue", "durable"] },
  { d: "M750 354 C750 384 480 380 480 410", groups: ["queue"] },
  { d: "M750 354 V410", groups: ["queue", "evals"] },
  { d: "M90 410 V354", groups: ["durable"], label: { x: 98, y: 386, text: "checkpoint" } },
  { d: "M150 498 V554", groups: ["gateway", "durable"], label: { x: 160, y: 530, text: "LLM calls" } },
  { d: "M270 498 C270 530 640 524 640 554", groups: [], label: { x: 420, y: 532, text: "tool calls" } },
  { d: "M250 630 V676", groups: ["gateway"] },
  { d: "M710 630 V676", groups: [] },
]

// Main path of one PR review, for the travelling packet.
export const PACKET_PATH = "M140 70 V166 H210 V310 H580 H750 V354 C750 384 175 380 175 410 V454 H150 V592"

export function ArchitectureDiagram({ active }: { active: Group | null }) {
  const on = (groups: Group[]) => (active ? groups.includes(active) : false)
  return (
    <svg
      viewBox="0 0 960 800"
      role="img"
      aria-labelledby="arch-title arch-desc"
      className="arch h-auto w-full"
      data-active={active ?? ""}
    >
      <title id="arch-title">Argus architecture</title>
      <desc id="arch-desc">
        GitHub webhooks reach the ingest service, which writes the event and an outbox row to
        Postgres. An outbox relay publishes jobs to Redis Streams. Agent, indexer and eval workers
        consume them; agents checkpoint every step in Postgres and reach models only through the LLM
        gateway and tools only through MCP servers. One OpenTelemetry trace spans every component.
      </desc>
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M0 0 L10 5 L0 10 z" className="arch-arrow" />
        </marker>
        <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="4" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      {EDGES.map((e, i) => (
        <g key={i} className="arch-edge-group" data-on={on(e.groups)}>
          <path d={e.d} className="arch-edge" markerEnd="url(#arrow)" pathLength={1} />
          {e.label && (
            <text x={e.label.x} y={e.label.y} className="arch-edge-label">
              {e.label.text}
            </text>
          )}
        </g>
      ))}

      {NODES.map((n) => (
        <g key={n.id} className="arch-node" data-on={on(n.groups)} data-external={n.external ? "" : undefined}>
          <rect x={n.x + 5} y={n.y + 5} width={n.w} height={n.h} rx={5} className="arch-node-shadow" />
          <rect x={n.x} y={n.y} width={n.w} height={n.h} rx={5} className="arch-node-box" />
          <text x={n.x + 14} y={n.y + (n.lines.length ? 26 : n.h / 2 + 5)} className="arch-node-title">
            {n.title}
          </text>
          {n.lines.map((line, i) => (
            <text key={i} x={n.x + 14} y={n.y + 48 + i * 18} className="arch-node-line">
              {line}
            </text>
          ))}
        </g>
      ))}

      <g className="arch-otel">
        <rect x={40} y={752} width={880} height={36} rx={5} />
        <text x={480} y={775} textAnchor="middle">
          OpenTelemetry: one trace across every box · Prometheus · Grafana
        </text>
      </g>

      <path id="packet-path" d={PACKET_PATH} fill="none" stroke="none" />
      <circle r={6} className="arch-packet" filter="url(#glow)" cx={140} cy={70} />
    </svg>
  )
}
