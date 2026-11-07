// Webhook burst: 300 signed webhooks/s against ingest (SPEC load table, row 1).
// Mix: 98% events Argus records but does not act on (issues), 2% pull_request opened
// (real reviews against the mock LLM), so the queue must drain and nothing may be lost.
//   docker run --rm -i --network host -e INGEST=http://localhost:8001 \
//     -e DURATION=60s -v "$PWD/deploy/load/k6:/s" grafana/k6:0.54.0 run /s/webhook_burst.js
import http from "k6/http";
import crypto from "k6/crypto";
import { check } from "k6";

const INGEST = __ENV.INGEST || "http://localhost:8001";
const SECRET = __ENV.SECRET || "dev-webhook-secret";
const RATE = parseInt(__ENV.RATE || "300");

export const options = {
  scenarios: {
    burst: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: __ENV.DURATION || "60s",
      preAllocatedVUs: 100,
      maxVUs: 400,
    },
  },
  thresholds: {
    "http_req_duration{kind:webhook}": ["p(99)<300"],
    http_req_failed: ["rate<0.001"],
  },
};

function uuid() {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

export default function () {
  const n = Math.floor(Math.random() * 1e9);
  const isPr = Math.random() < 0.02;
  const repo = { id: 7000 + (n % 50), full_name: `load-org-${n % 50}/svc`, private: false,
                 default_branch: "main", owner: { login: `load-org-${n % 50}` } };
  const payload = isPr
    ? { action: "opened", number: n % 100000,
        pull_request: { number: n % 100000, title: "Load test PR", body: "", draft: false,
                        head: { sha: crypto.sha1(String(n), "hex") } },
        repository: repo, installation: { id: 70000 + (n % 50) } }
    : { action: "opened", issue: { number: n }, repository: repo,
        installation: { id: 70000 + (n % 50) } };
  const body = JSON.stringify(payload);
  const res = http.post(`${INGEST}/webhooks/github`, body, {
    headers: {
      "content-type": "application/json",
      "x-github-event": isPr ? "pull_request" : "issues",
      "x-github-delivery": uuid(),
      "x-hub-signature-256": "sha256=" + crypto.hmac("sha256", SECRET, body, "hex"),
    },
    tags: { kind: "webhook" },
  });
  check(res, { "accepted": (r) => r.status === 202 });
}
