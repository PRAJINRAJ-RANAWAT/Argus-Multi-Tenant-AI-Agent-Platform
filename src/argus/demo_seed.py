"""Seed the public demo tenant with four weeks of realistic history.

``argus-seed-demo`` creates (or refreshes) the ``northwind`` organisation with four
repositories whose code is indexed for real (tree-sitter chunks and embeddings, so Q&A in
the demo answers with real citations), and about four weeks of activity: PR reviews that
succeeded, failed or were superseded, review comments waiting for approval, Q&A history
with cited files, model calls, daily usage against a budget, online judge scores, user
feedback and two flagged prompt-injection attempts.

The numbers are shaped like production runs on a Sonnet-class model, not like the mock
server: reviews take about a minute (p95 a few minutes), read 20-70k input tokens over
several LLM turns with prompt caching, and cost cents each.

Idempotent: a re-run deletes the demo tenant's seeded data and writes the same
deterministic data again (dates are relative to today, UTC). Other tenants are untouched.
"""

import argparse
import asyncio
import hashlib
import json
import math
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg
import httpx

from argus.github.client import GitHubClient
from argus.rag.embed import Embedder, HashingEmbedder, HttpEmbedder
from argus.rag.indexer import Indexer, RepoRef
from argus.runs import upsert_repo, upsert_tenant
from argus.testing.fakes import GITHUB_API, FakeGitHub

DEMO_LOGIN = "northwind"
DEMO_INSTALLATION = 990_001
DAYS = 28
SEED = 20261107

# Sonnet-class and Haiku-class list prices, USD per million tokens (input, output).
PRICES = {"claude-sonnet-4-5": (3.0, 15.0), "claude-haiku-4-5": (1.0, 5.0)}
CACHE_READ_DISCOUNT = 0.1  # cached input tokens bill at 10% of the input price
PROMPT_OLD, PROMPT_NEW = "4b1e9c02a7d1", "30503d4df5d3"
PROMPT_SWITCH_DAY = 9  # days ago the current review prompt shipped


# --- the demo repositories (real code, indexed for Q&A) -----------------------------

REPOS: dict[str, dict[str, str]] = {
    "northwind/checkout-service": {
        "app/payments.py": '''"""Charges customers through the payment gateway."""

import random
import time

from app.db import session
from app.gateway import GatewayClient, GatewayTimeout

MAX_ATTEMPTS = 5


def retry_with_backoff(fn, attempts=MAX_ATTEMPTS, base_delay=0.25):
    """Call fn, retrying timeouts with exponential backoff and full jitter."""
    for attempt in range(attempts):
        try:
            return fn()
        except GatewayTimeout:
            if attempt == attempts - 1:
                raise
            time.sleep(random.uniform(0, base_delay * 2 ** attempt))


def charge_customer(customer_id, amount_cents, idempotency_key):
    """Charge once per idempotency key, even if the request is retried."""
    client = GatewayClient()
    return retry_with_backoff(
        lambda: client.charge(customer_id, amount_cents, idempotency_key=idempotency_key)
    )


def refund(charge_id, amount_cents=None):
    client = GatewayClient()
    return client.refund(charge_id, amount_cents)
''',
        "app/orders.py": """from app.db import session
from app.models import Order, OrderLine
from app.payments import charge_customer


def create_order(cart, customer):
    order = Order(customer_id=customer.id, status="pending")
    for item in cart.items:
        order.lines.append(OrderLine(sku=item.sku, quantity=item.quantity, price=item.price))
    session.add(order)
    session.flush()
    charge_customer(customer.id, order.total_cents(), idempotency_key=f"order-{order.id}")
    order.status = "paid"
    session.commit()
    return order


def cancel_order(order_id):
    order = session.get(Order, order_id)
    if order is None:
        return None
    order.status = "cancelled"
    session.commit()
    return order
""",
        "app/webhooks.py": '''import hashlib
import hmac

from app.config import settings


def verify_signature(body: bytes, header: str) -> bool:
    """Verify the gateway's HMAC-SHA256 webhook signature in constant time."""
    expected = "sha256=" + hmac.new(settings.webhook_secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def handle_event(event):
    if event["type"] == "charge.refunded":
        mark_refunded(event["data"]["charge_id"])
    elif event["type"] == "charge.failed":
        notify_customer(event["data"]["customer_id"])
''',
        "app/cart.py": """from decimal import Decimal


class Cart:
    def __init__(self):
        self.items = []

    def add(self, sku, quantity, price):
        self.items.append(CartItem(sku, quantity, Decimal(price)))

    def total(self):
        return sum(item.price * item.quantity for item in self.items)


class CartItem:
    def __init__(self, sku, quantity, price):
        self.sku = sku
        self.quantity = quantity
        self.price = price
""",
        "README.md": "# checkout-service\n\nOrders, carts and payments for the Northwind store.\n"
        "Charges go through `app/payments.py`, which retries gateway timeouts with backoff.\n",
    },
    "northwind/web-storefront": {
        "src/api/client.ts": """export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, { credentials: "include", ...init })
  if (!res.ok) throw new ApiError(res.status, await res.text())
  return res.json() as Promise<T>
}

export function fetchProducts(category: string) {
  return request<Product[]>(`/products?category=${encodeURIComponent(category)}`)
}
""",
        "src/cart/useCart.ts": """import { useCallback, useState } from "react"

export function useCart() {
  const [items, setItems] = useState<CartItem[]>([])

  const add = useCallback((product: Product, quantity = 1) => {
    setItems((prev) => {
      const existing = prev.find((i) => i.sku === product.sku)
      if (existing) {
        return prev.map((i) => (i.sku === product.sku ? { ...i, quantity: i.quantity + quantity } : i))
      }
      return [...prev, { sku: product.sku, price: product.price, quantity }]
    })
  }, [])

  const total = items.reduce((sum, i) => sum + i.price * i.quantity, 0)
  return { items, add, total }
}
""",
        "src/checkout/CheckoutForm.tsx": """import { useState } from "react"
import { request } from "../api/client"

export function CheckoutForm({ cartId }: { cartId: string }) {
  const [submitting, setSubmitting] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setSubmitting(true)
    try {
      await request(`/carts/${cartId}/checkout`, { method: "POST" })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={submit}>
      <button disabled={submitting}>Place order</button>
    </form>
  )
}
""",
        "README.md": "# web-storefront\n\nThe customer-facing React app. API calls go through "
        "`src/api/client.ts`.\n",
    },
    "northwind/payments-api": {
        "internal/ledger/ledger.go": """package ledger

import (
	"context"
	"errors"
)

var ErrInsufficientFunds = errors.New("insufficient funds")

// Transfer moves amount between accounts inside one database transaction.
func (l *Ledger) Transfer(ctx context.Context, from, to string, amount int64) error {
	tx, err := l.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	balance, err := l.balance(ctx, tx, from)
	if err != nil {
		return err
	}
	if balance < amount {
		return ErrInsufficientFunds
	}
	if err := l.post(ctx, tx, from, -amount); err != nil {
		return err
	}
	if err := l.post(ctx, tx, to, amount); err != nil {
		return err
	}
	return tx.Commit()
}
""",
        "internal/api/ratelimit.go": """package api

import (
	"net/http"
	"golang.org/x/time/rate"
)

// RateLimit allows burst requests per client, refilled at r per second.
func RateLimit(r rate.Limit, burst int) func(http.Handler) http.Handler {
	limiters := newLimiterStore(r, burst)
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
			if !limiters.get(clientKey(req)).Allow() {
				http.Error(w, "too many requests", http.StatusTooManyRequests)
				return
			}
			next.ServeHTTP(w, req)
		})
	}
}
""",
        "README.md": "# payments-api\n\nThe double-entry ledger behind Northwind payments.\n",
    },
    "northwind/infra": {
        "deploy/README.md": "# Deploying\n\nMerges to main deploy automatically: the pipeline builds the "
        "image, runs migrations, rolls the services one at a time and checks /healthz.\n"
        "Roll back by re-deploying the previous image tag.\n",
        "terraform/database.tf": """resource "aws_db_instance" "main" {
  identifier              = "northwind-main"
  engine                  = "postgres"
  engine_version          = "16.3"
  instance_class          = "db.r6g.large"
  backup_retention_period = 14
  multi_az                = true
  deletion_protection     = true
}
""",
    },
}

QUESTIONS: dict[str, list[tuple[str, str]]] = {
    "northwind/checkout-service": [
        ("How do we retry failed payment gateway calls?", "retry_with_backoff"),
        ("Where do we make sure a customer is not charged twice?", "charge_customer"),
        ("How are webhook signatures from the gateway verified?", "verify_signature"),
        ("What happens when an order is cancelled?", "cancel_order"),
        ("How is the cart total calculated?", "Cart.total"),
    ],
    "northwind/web-storefront": [
        ("How does the storefront call the backend API?", "request"),
        ("How are items added to the cart?", "useCart"),
        ("What stops a customer from submitting checkout twice?", "CheckoutForm"),
    ],
    "northwind/payments-api": [
        ("How does a transfer avoid overdrawing an account?", "Transfer"),
        ("Where is API rate limiting implemented?", "RateLimit"),
    ],
    "northwind/infra": [
        ("How do I roll back a bad deploy?", ""),
        ("How long are database backups kept?", ""),
    ],
}

FINDINGS: dict[str, list[tuple[str, str, int, str]]] = {
    "northwind/checkout-service": [
        (
            "Missing idempotency key",
            "app/orders.py",
            12,
            "`charge_customer` is retried by the job runner without a stable key; a retry after a "
            "timeout can charge twice. Derive the key from the order id before the first attempt.",
        ),
        (
            "N+1 query",
            "app/orders.py",
            8,
            "Each `OrderLine` loads its product lazily inside the loop; prefetch the products in "
            "one query.",
        ),
        (
            "Missing None check",
            "app/orders.py",
            20,
            "`session.get` returns None for unknown ids, and the caller dereferences the result.",
        ),
        (
            "Unbounded retry",
            "app/payments.py",
            14,
            "The new loop has no upper bound when `attempts` comes from config; cap it.",
        ),
        (
            "Float money arithmetic",
            "app/cart.py",
            12,
            "Totals mix `float` and `Decimal`; keep prices as integer cents end to end.",
        ),
    ],
    "northwind/web-storefront": [
        (
            "Double submit",
            "src/checkout/CheckoutForm.tsx",
            9,
            "The button is disabled only after the request starts; a fast double click sends two "
            "checkout requests. Disable synchronously and send an idempotency key.",
        ),
        (
            "Unescaped query parameter",
            "src/api/client.ts",
            14,
            "`category` is interpolated without `encodeURIComponent` in the new search call.",
        ),
        (
            "Missing error state",
            "src/cart/useCart.ts",
            6,
            "Failures from `add` are swallowed; surface them so the cart does not silently drift.",
        ),
    ],
    "northwind/payments-api": [
        (
            "Race condition",
            "internal/ledger/ledger.go",
            17,
            "The balance is read without `SELECT ... FOR UPDATE`; two concurrent transfers can both "
            "pass the check. Lock the source row or use a serializable transaction.",
        ),
        (
            "Missing tests",
            "internal/api/ratelimit.go",
            9,
            "The new limiter store has no tests for eviction or concurrent access.",
        ),
    ],
    "northwind/infra": [
        (
            "Deletion protection disabled",
            "terraform/database.tf",
            8,
            "This change turns off `deletion_protection` on the production database.",
        ),
        (
            "Hardcoded secret",
            "terraform/variables.tf",
            3,
            "A database password is committed in plain text; move it to the secret manager. "
            "(The value was redacted from this comment.)",
        ),
    ],
}

PR_TITLES = {
    "northwind/checkout-service": [
        "Retry gateway timeouts",
        "Add partial refunds",
        "Speed up order listing",
        "Cancel unpaid orders after 24h",
        "Move cart totals to Decimal",
        "Webhook handler for disputes",
        "Bulk order export",
    ],
    "northwind/web-storefront": [
        "Checkout redesign",
        "Product search filters",
        "Persist cart in local storage",
        "Accessible quantity picker",
        "Lazy-load product images",
    ],
    "northwind/payments-api": [
        "Ledger transfer API",
        "Per-client rate limits",
        "Idempotent payouts",
        "Reconciliation job",
    ],
    "northwind/infra": [
        "Bump Postgres to 16.3",
        "Add read replica",
        "Rotate database credentials",
        "Alert on queue lag",
    ],
}

FAILURES = [
    "BudgetExceededError: run budget exceeded: max_steps=25",
    "AllModelsUnavailableError: no model available for tier 'large' (fallback:claude-haiku-4-5_error_529)",
    "LLMError: LLM error 504: gateway deadline exceeded",
]


# --- generation ------------------------------------------------------------------------


@dataclass
class Calls:
    """Per-run model call breakdown."""

    turns: list[tuple[int, int, int]]  # (input, output, cached) per LLM turn
    model: str

    @property
    def input(self) -> int:
        return sum(t[0] for t in self.turns)

    @property
    def output(self) -> int:
        return sum(t[1] for t in self.turns)

    def cost(self) -> float:
        p_in, p_out = PRICES[self.model]
        total = 0.0
        for inp, out, cached in self.turns:
            total += (
                (inp - cached) * p_in + cached * p_in * CACHE_READ_DISCOUNT + out * p_out
            ) / 1e6
        return total


def _lognormal(rng: random.Random, median: float, sigma: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, math.exp(rng.gauss(math.log(median), sigma))))


def _review_calls(rng: random.Random) -> Calls:
    turns = []
    # Each turn re-sends the conversation: system prompt, tools, PR text and diff, then the
    # tool results so far. Summed over turns this is ~30-90k input tokens per review.
    context = rng.randint(4_500, 8_000)
    for i in range(rng.randint(3, 6)):
        context += rng.randint(900, 4_200)
        out = rng.randint(120, 450) if i else rng.randint(150, 400)
        cached = int(context * rng.uniform(0.55, 0.8)) if i else 0  # stable prefix after turn 1
        turns.append((context, out, cached))
    turns[-1] = (turns[-1][0], rng.randint(700, 2_600), turns[-1][2])  # the written review
    return Calls(turns, "claude-sonnet-4-5")


def _qa_calls(rng: random.Random) -> Calls:
    turns = []
    context = rng.randint(3_500, 6_000)
    for i in range(rng.randint(2, 4)):
        context += rng.randint(1_200, 4_500)
        cached = int(context * rng.uniform(0.5, 0.75)) if i else 0
        turns.append((context, rng.randint(80, 250), cached))
    turns[-1] = (turns[-1][0], rng.randint(220, 750), turns[-1][2])
    return Calls(turns, "claude-sonnet-4-5")


def _when(rng: random.Random, day: date) -> datetime:
    """A working-hours timestamp (Northwind is spread over a few time zones)."""
    hour = min(23, max(0, int(rng.gauss(14, 3.5))))
    return datetime(
        day.year, day.month, day.day, hour, rng.randint(0, 59), rng.randint(0, 59), tzinfo=UTC
    )


def _sha(rng: random.Random) -> str:
    return f"{rng.getrandbits(160):040x}"


def _review_markdown(rng: random.Random, repo: str, sha: str, flagged: bool) -> tuple[str, int]:
    pool = FINDINGS[repo]
    n = rng.choices([0, 1, 2, 3], weights=[2, 4, 3, 1])[0]
    picked = rng.sample(pool, min(n, len(pool)))
    lines = [f"## Argus review for `{sha[:12]}`", ""]
    if flagged:
        lines += [
            "> **Note:** Argus found text in this pull request that looks like instructions "
            "aimed at an AI reviewer (override instructions, approve or skip). It was treated "
            "as data, and this review ran with a reduced tool set.",
            "",
        ]
    if not picked:
        lines += [
            "No issues found in this change. The new code paths are covered by the added tests."
        ]
    else:
        lines += ["### Findings", ""]
        for title, path, line, detail in picked:
            lines.append(f"- **{title}** (`{path}:{line}`): {detail}")
    lines += ["", "<sub>Automated review by Argus.</sub>"]
    return "\n".join(lines), len(picked)


def _repo_name(repo_id: uuid.UUID, repos: dict[str, tuple[uuid.UUID, str]]) -> str:
    return next(name for name, (rid, _) in repos.items() if rid == repo_id)


async def _index_repo(
    pool: asyncpg.Pool,
    embedder: Embedder,
    ref: RepoRef,
    files: dict[str, str],
    sha: str,
    when: datetime,
) -> None:
    gh = FakeGitHub(files=dict(files))

    async def token(_: int) -> str:
        return "ghs_fake_1_1"

    async with httpx.AsyncClient(transport=gh.transport()) as http:
        github = GitHubClient(http, token, GITHUB_API)
        await Indexer(pool, github, embedder).full_index(ref, sha, when, "main")


async def _wipe(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> None:
    async with pool.acquire() as conn, conn.transaction():
        for sql in (
            "DELETE FROM llm_calls WHERE tenant_id = $1",
            "DELETE FROM runs WHERE tenant_id = $1",  # cascades steps, approvals, evals, feedback
            "DELETE FROM side_effects WHERE tenant_id = $1",
            "DELETE FROM tenant_usage_daily WHERE tenant_id = $1",
            "DELETE FROM code_files WHERE tenant_id = $1",  # cascades chunks and refs
            "DELETE FROM embeddings WHERE tenant_id = $1",
            "DELETE FROM repo_index_state WHERE tenant_id = $1",
            "DELETE FROM qa_cache WHERE tenant_id = $1",
        ):
            await conn.execute(sql, tenant_id)


async def seed_demo(
    pool: asyncpg.Pool, embedder: Embedder, *, today: date | None = None
) -> dict[str, Any]:
    """Create or refresh the demo tenant. Returns a summary of what was written."""
    rng = random.Random(SEED)
    today = today or datetime.now(UTC).date()
    start = today - timedelta(days=DAYS - 1)
    tenant_id = await upsert_tenant(pool, DEMO_INSTALLATION, DEMO_LOGIN)
    await _wipe(pool, tenant_id)

    repos: dict[str, tuple[uuid.UUID, str]] = {}
    for i, (name, files) in enumerate(REPOS.items()):
        repo_id = await upsert_repo(pool, tenant_id, DEMO_INSTALLATION * 10 + i, name, False)
        sha = hashlib.sha1(f"{name}@main".encode(), usedforsecurity=False).hexdigest()
        ref = RepoRef(tenant_id, repo_id, DEMO_INSTALLATION, name)
        await _index_repo(
            pool, embedder, ref, files, sha, datetime.combine(start, datetime.min.time(), UTC)
        )
        repos[name] = (repo_id, sha)

    # Symbol -> (path, start, end) per repo, from the real index, for Q&A citations.
    chunk_rows = await pool.fetch(
        "SELECT r.full_name, c.symbol, c.path, c.start_line, c.end_line FROM code_chunks c"
        " JOIN repos r ON r.id = c.repo_id WHERE c.tenant_id = $1",
        tenant_id,
    )
    symbols: dict[tuple[str, str], tuple[str, int, int]] = {}
    paths: dict[str, list[tuple[str, int, int]]] = {}
    for row in chunk_rows:
        if row["symbol"]:
            symbols[(row["full_name"], row["symbol"])] = (
                row["path"],
                row["start_line"],
                row["end_line"],
            )
        paths.setdefault(row["full_name"], []).append(
            (row["path"], row["start_line"], row["end_line"])
        )

    runs: list[tuple[Any, ...]] = []
    steps: list[tuple[Any, ...]] = []
    calls: list[tuple[Any, ...]] = []
    approvals: list[tuple[Any, ...]] = []
    evals: list[tuple[Any, ...]] = []
    feedback: list[tuple[Any, ...]] = []
    usage: dict[date, list[float]] = {}
    pr_counter = {name: 1180 + 97 * i for i, name in enumerate(REPOS)}
    repo_weights = [5, 3, 2, 1]
    flagged_days = {DAYS - 6, DAYS - 17}  # two injection attempts
    pending_left = 3

    def add_steps(
        run_id: uuid.UUID,
        c: Calls,
        tools: list[str],
        t0: datetime,
        dur: float,
        final_tool: str | None,
    ) -> None:
        seq = 0
        t = t0
        per = dur / (len(c.turns) + len(tools) + 1)
        tool_iter = iter(tools)
        for inp, out, cached in c.turns:
            steps.append(
                (run_id, seq, "llm_call", "gateway:large", inp, out, t, t + timedelta(seconds=per))
            )
            calls.append((run_id, c.model, inp, out, cached, t, per))
            seq += 1
            t += timedelta(seconds=per)
            tool = next(tool_iter, None)
            if tool:
                steps.append(
                    (run_id, seq, "tool_call", tool, 0, 0, t, t + timedelta(seconds=per * 0.3))
                )
                seq += 1
                t += timedelta(seconds=per * 0.3)
        if final_tool:
            steps.append(
                (run_id, seq, "tool_call", final_tool, 0, 0, t, t + timedelta(seconds=0.6))
            )

    for d in range(DAYS):
        day = start + timedelta(days=d)
        weekday = day.weekday() < 5
        n_reviews = rng.randint(7, 13) if weekday else rng.randint(0, 3)
        n_qa = rng.randint(2, 6) if weekday else rng.randint(0, 2)
        days_ago = DAYS - 1 - d
        prompt = PROMPT_NEW if days_ago < PROMPT_SWITCH_DAY else PROMPT_OLD

        for k in range(n_reviews):
            name = rng.choices(list(REPOS), weights=repo_weights)[0]
            repo_id, _ = repos[name]
            pr_counter[name] += rng.choice([0, 1, 1, 2])
            pr = pr_counter[name]
            head = _sha(rng)
            created = _when(rng, day)
            run_id = uuid.UUID(int=rng.getrandbits(128))
            c = _review_calls(rng)
            dur = _lognormal(rng, 72, 0.45, 24, 300)
            started = created + timedelta(seconds=rng.uniform(0.3, 2.5))
            finished: datetime | None = started + timedelta(seconds=dur)
            roll = rng.random()
            flagged = d in flagged_days and k == 0
            tools = rng.sample(
                [
                    "get_pr_diff",
                    "list_changed_files",
                    "read_file",
                    "read_file",
                    "search_code",
                    "find_callers",
                    "get_ci_status",
                ],
                k=min(len(c.turns), 5),
            )
            tools[0] = "get_pr_diff"
            if roll < 0.06:
                status = "failed"
            elif roll < 0.17:
                status = "superseded"
            else:
                status = "succeeded"
            if flagged:
                status = "succeeded"
                tools = ["get_pr_diff", "list_changed_files"][: len(c.turns)]
            review, n_findings = _review_markdown(rng, name, head, flagged)
            guard = {
                "injection_flagged": flagged,
                "rules": ["override_instructions", "approve_or_skip"] if flagged else [],
                "sources": ["pr_description"] if flagged else [],
                "output_redactions": {"secret": 1} if "Hardcoded secret" in review else {},
                "echoes_removed": 1 if flagged else 0,
            }
            result: dict[str, Any] | None
            error = None
            if status == "succeeded":
                result = {
                    "comment_id": str(rng.randint(10**9, 2 * 10**9)),
                    "model": c.model,
                    "comment_reused": False,
                    "prompt_version": prompt,
                    "review": review,
                    "steps_replayed": rng.choice([0] * 30 + [2, 3]),
                    "steps": len(c.turns) * 2,
                    "findings": n_findings,
                    "guardrails": guard,
                }
            elif status == "superseded":
                c = Calls(c.turns[: rng.randint(1, 3)], c.model)
                finished = started + timedelta(seconds=dur * rng.uniform(0.2, 0.6))
                result = {"superseded_by": str(uuid.UUID(int=rng.getrandbits(128)))}
            else:  # failed
                c = Calls(c.turns[: rng.randint(2, len(c.turns))], c.model)
                error = rng.choice(FAILURES)
                result = None
            runs.append(
                (
                    run_id,
                    repo_id,
                    "pr_review",
                    f"review:{repo_id}:{pr}:{head}",
                    pr,
                    head,
                    status,
                    c.input,
                    c.output,
                    c.cost(),
                    error,
                    result,
                    None,
                    created,
                    started,
                    finished,
                )
            )
            final = "post_review_comment" if status == "succeeded" else None
            add_steps(
                run_id,
                c,
                tools,
                started,
                (finished - started).total_seconds() if finished else dur,
                final,
            )
            u = usage.setdefault(day, [0, 0.0])
            u[0] += c.input + c.output
            u[1] += c.cost()
            if status == "succeeded" and rng.random() < 0.18:
                base = 4.3 if prompt == PROMPT_NEW else 3.8
                score = max(2, min(5, round(rng.gauss(base, 0.7))))
                evals.append((run_id, prompt, score, finished))
            if status == "succeeded" and rng.random() < 0.16:
                feedback.append((run_id, "up" if rng.random() < 0.86 else "down", finished))
            if status == "succeeded" and days_ago in (3, 5, 8) and k == 1:
                approvals.append(
                    (
                        run_id,
                        "approved",
                        {"repo": name, "pr": pr, "body": review},
                        started + timedelta(seconds=dur * 0.9),
                        "user:maya-chen",
                        started + timedelta(seconds=dur, minutes=rng.randint(4, 90)),
                    )
                )

        for _ in range(n_qa):
            name = rng.choices(list(REPOS), weights=[4, 2, 2, 1])[0]
            repo_id, sha = repos[name]
            question, symbol = rng.choice(QUESTIONS[name])
            created = _when(rng, day)
            run_id = uuid.UUID(int=rng.getrandbits(128))
            c = _qa_calls(rng)
            dur = _lognormal(rng, 11, 0.4, 4, 40)
            started = created + timedelta(seconds=rng.uniform(0.2, 1.5))
            finished = started + timedelta(seconds=dur)
            hit = symbols.get((name, symbol)) if symbol else None
            if hit is None:
                hit = paths[name][0] if paths.get(name) else None
            citations = []
            answer = "I could not find this in the indexed code."
            if hit:
                path, s, e = hit
                cite = f"{path}:{s}-{e}"
                citations = [
                    {
                        "citation": f"{cite}@{sha[:12]}",
                        "path": path,
                        "lines": [s, e],
                        "url": f"https://github.com/{name}/blob/{sha}/{path}#L{s}-L{e}",
                    }
                ]
                answer = f"It is handled in `{symbol or path}` ({cite}). " + {
                    "retry_with_backoff": "Timeouts are retried up to 5 times with "
                    "exponential backoff and full jitter; the last failure is re-raised.",
                    "charge_customer": "Every charge carries an idempotency key derived "
                    "from the order, so a retried request cannot charge twice.",
                    "verify_signature": "The HMAC-SHA256 of the raw body is compared "
                    "with `hmac.compare_digest`, so the check is constant time.",
                }.get(symbol, "The relevant logic is in the cited lines.")
            result = {
                "answer": answer,
                "citations": citations,
                "unverified_citations": [],
                "index_commit": sha,
                "model": c.model,
                "guardrails": {
                    "injection_flagged": False,
                    "rules": [],
                    "sources": [],
                    "output_redactions": {},
                    "echoes_removed": 0,
                },
            }
            runs.append(
                (
                    run_id,
                    repo_id,
                    "qa",
                    f"qa:seed:{run_id}",
                    None,
                    None,
                    "succeeded",
                    c.input,
                    c.output,
                    c.cost(),
                    None,
                    result,
                    json.dumps({"question": question, "role": "member"}),
                    created,
                    started,
                    finished,
                )
            )
            add_steps(
                run_id,
                c,
                ["search_code", "read_file", "find_callers"][: len(c.turns)],
                started,
                dur,
                None,
            )
            u = usage.setdefault(day, [0, 0.0])
            u[0] += c.input + c.output
            u[1] += c.cost()

    # The three most recent clean reviews are waiting for a person to approve the
    # comment (deterministic whatever weekday it is).
    latest = sorted(
        (
            i
            for i, r in enumerate(runs)
            if r[2] == "pr_review"
            and r[6] == "succeeded"
            and not r[11]["guardrails"]["injection_flagged"]
        ),
        key=lambda i: runs[i][13],
    )[-pending_left:]
    for i in latest:
        r = runs[i]
        approvals.append(
            (
                r[0],
                "pending",
                {"repo": _repo_name(r[1], repos), "pr": r[4], "body": r[11]["review"]},
                r[15],
                None,
                None,
            )
        )
        runs[i] = (*r[:6], "waiting_approval", *r[7:11], None, r[12], r[13], r[14], None)
        steps[:] = [st for st in steps if not (st[0] == r[0] and st[3] == "post_review_comment")]
    evals[:] = [e for e in evals if e[0] not in {runs[i][0] for i in latest}]
    feedback[:] = [f for f in feedback if f[0] not in {runs[i][0] for i in latest}]

    async with pool.acquire() as conn, conn.transaction():
        await conn.executemany(
            """
            INSERT INTO runs (id, tenant_id, repo_id, agent_type, idempotency_key, pr_number,
                head_sha, status, attempts, lease_version, input_tokens, output_tokens, cost_usd,
                error, result, input, created_at, updated_at, started_at, finished_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 1, 1, $9, $10, $11, $12, $13::jsonb,
                    $14::jsonb, $15::timestamptz, coalesce($17::timestamptz, $16::timestamptz),
                    $16::timestamptz, $17::timestamptz)
            """,
            [
                (
                    r[0],
                    tenant_id,
                    r[1],
                    r[2],
                    r[3],
                    r[4],
                    r[5],
                    r[6],
                    r[7],
                    r[8],
                    r[9],
                    r[10],
                    json.dumps(r[11]) if r[11] is not None else None,
                    r[12],
                    r[13],
                    r[14],
                    r[15],
                )
                for r in runs
            ],
        )
        await conn.executemany(
            """
            INSERT INTO run_steps (run_id, seq, kind, name, input_hash, status, output,
                lease_version, input_tokens, output_tokens, started_at, finished_at, tenant_id)
            VALUES ($1, $2, $3, $4, 'seed', 'done', '{}'::jsonb, 1, $5, $6, $7, $8, $9)
            """,
            [(*s, tenant_id) for s in steps],
        )
        await conn.executemany(
            """
            INSERT INTO llm_calls (tenant_id, run_id, agent, tier, provider, model, route_reason,
                status, attempts, input_tokens, output_tokens, cached_tokens, cost_usd,
                latency_ms, created_at)
            VALUES ($1, $2, 'seed', 'large', 'anthropic', $3, $4, 'ok', 1, $5, $6, $7, $8, $9, $10)
            """,
            [
                (
                    tenant_id,
                    run_id,
                    model,
                    "primary" if (inp + out) % 47 else "fallback:claude-sonnet-4-5_error_529",
                    inp,
                    out,
                    cached,
                    (
                        (inp - cached) * PRICES[model][0]
                        + cached * PRICES[model][0] * CACHE_READ_DISCOUNT
                        + out * PRICES[model][1]
                    )
                    / 1e6,
                    int(per * 1000 * 0.85),
                    t,
                )
                for run_id, model, inp, out, cached, t, per in calls
            ],
        )
        await conn.executemany(
            """
            INSERT INTO approvals (tenant_id, run_id, step_key, tool, preview, status,
                requested_at, decided_by, decided_at)
            VALUES ($1, $2, 'post_review_comment', 'post_review_comment', $4::jsonb, $3, $5, $6,
                    $7)
            """,
            [(tenant_id, a[0], a[1], json.dumps(a[2]), a[3], a[4], a[5]) for a in approvals],
        )
        await conn.executemany(
            """
            INSERT INTO eval_samples (run_id, tenant_id, agent, prompt_version, status, judge_score,
                created_at, scored_at)
            VALUES ($1, $2, 'pr_review', $3, 'done', $4, $5::timestamptz,
                    $5::timestamptz + interval '2 minutes')
            """,
            [(e[0], tenant_id, e[1], e[2], e[3]) for e in evals],
        )
        await conn.executemany(
            """
            INSERT INTO run_feedback (tenant_id, run_id, actor, rating, created_at)
            VALUES ($1, $2, $3, $4, $5::timestamptz + interval '20 minutes')
            """,
            [(tenant_id, f[0], f"user:seed-{i % 9}", f[1], f[2]) for i, f in enumerate(feedback)],
        )
        await conn.executemany(
            """
            INSERT INTO tenant_usage_daily (tenant_id, day, used, reserved, cost_usd)
            VALUES ($1, $2, $3, 0, $4)
            """,
            [(tenant_id, day, int(v[0]), round(v[1], 6)) for day, v in sorted(usage.items())],
        )
        # A small daily budget: real history fits under it, and it also caps what demo
        # visitors can spend on questions.
        await conn.execute(
            """
            INSERT INTO tenant_budgets (tenant_id, daily_tokens, alert_pct) VALUES ($1, $2, 80)
            ON CONFLICT (tenant_id) DO UPDATE SET daily_tokens = EXCLUDED.daily_tokens,
                alert_pct = 80, updated_at = now()
            """,
            tenant_id,
            2_500_000,
        )
        await conn.execute(
            """
            INSERT INTO tenant_settings (tenant_id, require_approval_for)
            VALUES ($1, '{post_review_comment}')
            ON CONFLICT (tenant_id) DO UPDATE SET require_approval_for = '{post_review_comment}'
            """,
            tenant_id,
        )
    statuses: dict[str, int] = {}
    for r in runs:
        statuses[r[6]] = statuses.get(r[6], 0) + 1
    return {
        "tenant": DEMO_LOGIN,
        "repos": len(repos),
        "runs": len(runs),
        "statuses": statuses,
        "reviews": sum(1 for r in runs if r[2] == "pr_review"),
        "questions": sum(1 for r in runs if r[2] == "qa"),
        "pending_approvals": sum(1 for a in approvals if a[1] == "pending"),
        "flagged": sum(
            1 for r in runs if r[11] and r[11].get("guardrails", {}).get("injection_flagged")
        ),
        "steps": len(steps),
        "llm_calls": len(calls),
        "cost_usd": round(sum(v[1] for v in usage.values()), 2),
        "tokens": int(sum(v[0] for v in usage.values())),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Create or refresh the public demo tenant.")
    p.add_argument(
        "--hashing-embedder",
        action="store_true",
        help="embed with the built-in hashing embedder instead of the configured one",
    )
    args = p.parse_args()

    async def run() -> dict[str, Any]:
        from argus.config import get_settings
        from argus.db.migrate import default_migrations_dir
        from argus.db.migrate import run as migrate
        from argus.db.pool import create_pool

        s = get_settings()
        await migrate(s.database_url, default_migrations_dir())
        pool = await create_pool(s.database_url, max_size=5)
        try:
            async with httpx.AsyncClient(timeout=60) as http:
                embedder: Embedder = (
                    HashingEmbedder(s.embed_model)
                    if args.hashing_embedder
                    else HttpEmbedder(
                        http,
                        base_url=s.embed_base_url or s.llm_base_url,
                        api_key=s.embed_api_key.get_secret_value()
                        or s.llm_api_key.get_secret_value(),
                        model=s.embed_model,
                    )
                )
                return await seed_demo(pool, embedder)
        finally:
            await pool.close()

    print(json.dumps(asyncio.run(run()), indent=2))
