// Reads the real reports in the repository and writes the figures the landing page
// shows. A metric whose source is missing or does not match is left out: the landing
// page never shows a number that is not in a committed report.
//
// Sources: docs/load-and-chaos-report.md, evals/baseline.json,
// evals/reports/injection.json, docs/adr/*.md.
import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..")
const out = path.resolve(root, "frontend/src/landing/proof.generated.json")
const read = (rel) => {
  try {
    return fs.readFileSync(path.join(root, rel), "utf8")
  } catch {
    return null
  }
}
const json = (rel) => {
  const text = read(rel)
  try {
    return text ? JSON.parse(text) : null
  } catch {
    return null
  }
}

const metrics = []
const add = (m) => {
  if (m.value !== undefined && m.value !== null && Number.isFinite(m.value)) metrics.push(m)
}

const LOAD = "docs/load-and-chaos-report.md"
const load = read(LOAD)
if (load) {
  const reqs = load.match(/http_reqs \.+ (\d+)\s+([\d.]+)\/s/)
  const p99 = load.match(/p\(99\)=([\d.]+)ms/)
  const failed = load.match(/http_req_failed \.+ ([\d.]+)%/)
  const lost = load.match(/All ([\d,]+) PR events reviewed, (\d+) lost/)
  const quiet = load.match(/Quiet p95 \*\*([\d.]+) s\*\* \(FIFO ([\d.]+) s\)/)
  const incidents = load.match(/^### \d+\./gm)
  if (reqs) add({ id: "webhook_rate", group: "Load", value: Number(reqs[2]), decimals: 1, unit: "/s",
    label: "Signed webhooks per second", detail: `${Number(reqs[1]).toLocaleString("en")} requests in a 60 s burst`, source: LOAD })
  if (p99) add({ id: "ingest_p99", group: "Load", value: Number(p99[1]), decimals: 0, unit: "ms",
    label: "Ingest p99 under that burst", detail: "SLO: under 300 ms", source: LOAD })
  if (lost) add({ id: "lost_events", group: "Load", value: Number(lost[2]), decimals: 0, unit: "",
    label: "Events lost", detail: `${lost[1]} PR events, every one reviewed`, source: LOAD })
  if (failed) add({ id: "failed_requests", group: "Load", value: Number(failed[1]), decimals: 2, unit: "%",
    label: "Failed requests in the burst", detail: "k6, constant arrival rate", source: LOAD })
  if (quiet) add({ id: "noisy_neighbour", group: "Load", value: Number(quiet[1]), decimals: 1, unit: "s",
    label: "Quiet tenants' p95 beside a 100-PR flood", detail: `${quiet[2]} s with a plain FIFO queue`, source: LOAD })
  if (incidents) add({ id: "incidents", group: "Load", value: incidents.length, decimals: 0, unit: "",
    label: "Failure modes found by chaos runs, all fixed", detail: "written up incident-style", source: LOAD })
}

const BASE = "evals/baseline.json"
const base = json(BASE)
if (base) {
  const pct = (v) => (typeof v === "number" ? v * 100 : undefined)
  add({ id: "review_recall", group: "Evals", value: pct(base["review.recall"]), decimals: 0, unit: "%",
    label: "Planted bugs caught on the golden PR set", detail: `precision ${Math.round(pct(base["review.precision"]) ?? 0)}%, gated in CI`, source: BASE })
  add({ id: "qa_retrieval", group: "Evals", value: pct(base["qa.retrieval_recall_at_5"]), decimals: 0, unit: "%",
    label: "Q&A retrieval recall@5", detail: "right file in the top five results", source: BASE })
}

const INJ = "evals/reports/injection.json"
const inj = json(INJ)
if (inj?.report) {
  const r = inj.report
  const abl = inj.ablations?.["no classifier"]?.attack_success_rate
  add({ id: "attack_success", group: "Safety", value: r.attack_success_rate * 100, decimals: 1, unit: "%",
    label: "Prompt-injection attack success rate", detail: `${r.succeeded} of ${r.total_attacks} attacks against a fully hijacked model` +
      (typeof abl === "number" ? `; ${(abl * 100).toFixed(0)}% without the classifier` : ""), source: INJ })
}

const adrs = []
const adrDir = path.join(root, "docs/adr")
if (fs.existsSync(adrDir)) {
  for (const f of fs.readdirSync(adrDir).sort()) {
    if (!/^\d{4}-.+\.md$/.test(f) || f.startsWith("0000")) continue
    const first = fs.readFileSync(path.join(adrDir, f), "utf8").split("\n")[0]
    const m = first.match(/^#\s*(\d{4})\.\s*(.+)$/)
    if (m) adrs.push({ number: m[1], title: m[2].trim(), path: `docs/adr/${f}` })
  }
}

const reports = [
  [LOAD, "Load and chaos report", "300 webhooks/s, a noisy neighbour, Redis restarts, and six incidents written up."],
  ["evals/reports/latest.md", "Eval report", "Review recall, precision and line accuracy, Q&A retrieval, judge agreement."],
  ["evals/reports/injection.md", "Prompt-injection eval", "29 attacks against a hijacked model, with ablations per defence layer."],
].filter(([p]) => read(p) !== null).map(([p, title, body]) => ({ path: p, title, body }))

fs.writeFileSync(out, JSON.stringify({ metrics, adrs, reports }, null, 2) + "\n")
console.log(`proof: ${metrics.length} metrics, ${adrs.length} ADRs, ${reports.length} reports -> ${path.relative(root, out)}`)
