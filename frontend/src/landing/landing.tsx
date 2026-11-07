import { useEffect, useLayoutEffect, useRef, useState } from "react"
import gsap from "gsap"
import { MotionPathPlugin } from "gsap/MotionPathPlugin"
import { ScrollTrigger } from "gsap/ScrollTrigger"
import { SplitText } from "gsap/SplitText"
import Lenis from "lenis"
import {
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  CircleCheck,
  FileText,
  Gauge,
  GitPullRequest,
  Play,
  ShieldCheck,
  Sparkles,
} from "lucide-react"

import { ThemeToggle } from "@/components/theme-toggle"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

import { ArchitectureDiagram, type Group } from "./architecture"
import {
  AUTHOR,
  DEMO_VIDEO_POSTER,
  DEMO_VIDEO_URL,
  GITHUB_URL,
  LINKEDIN_URL,
  blob,
  tree,
} from "./config"
import proof from "./proof.generated.json"

gsap.registerPlugin(ScrollTrigger, SplitText, MotionPathPlugin)

interface Metric {
  id: string
  group: string
  value: number
  decimals: number
  unit: string
  label: string
  detail: string
  source: string
}

const METRICS = proof.metrics as Metric[]
const ADRS = proof.adrs as { number: string; title: string; path: string }[]
const REPORTS = proof.reports as { path: string; title: string; body: string }[]

const YEAR = new Date().getFullYear()

const reduceMotion = () =>
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches

function GitHubMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" aria-hidden className={className} fill="currentColor">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
    </svg>
  )
}

function Logo() {
  return (
    <a href="/" className="flex items-center gap-2" aria-label="Argus home">
      <span className="grid size-8 place-items-center rounded-md border-[1.5px] border-border bg-ink shadow-hard-sm">
        <span className="size-3.5 rounded-full border-[3px] border-neon" />
      </span>
      <span className="display text-lg">Argus</span>
    </a>
  )
}

// --- motion --------------------------------------------------------------------------

/** Lenis smooth scrolling driven by GSAP's ticker, so ScrollTrigger stays in sync.
 *  Off for reduced-motion users (native scrolling, no animations). */
function useSmoothScroll() {
  useEffect(() => {
    if (reduceMotion()) return
    const lenis = new Lenis({ duration: 1.1, smoothWheel: true })
    lenis.on("scroll", ScrollTrigger.update)
    const tick = (time: number) => lenis.raf(time * 1000)
    gsap.ticker.add(tick)
    gsap.ticker.lagSmoothing(0)
    const onClick = (e: MouseEvent) => {
      const a = (e.target as HTMLElement).closest<HTMLAnchorElement>("a[href^='#']")
      if (!a) return
      const target = document.querySelector(a.getAttribute("href")!)
      if (!target) return
      e.preventDefault()
      lenis.scrollTo(target as HTMLElement, { offset: -72 })
    }
    document.addEventListener("click", onClick)
    return () => {
      document.removeEventListener("click", onClick)
      gsap.ticker.remove(tick)
      lenis.destroy()
    }
  }, [])
}

function useLandingMotion(root: React.RefObject<HTMLElement | null>) {
  useLayoutEffect(() => {
    if (!root.current || reduceMotion()) return
    const cleanup: (() => void)[] = []
    const ctx = gsap.context(() => {
      // Hero: the headline rises word by word, then everything else settles in.
      // autoSplit re-splits once the web fonts load or the width changes, so lines stay right.
      // autoSplit re-splits once the web fonts load or the width changes, so lines stay right.
      SplitText.create(".hero-title", {
        type: "words,lines",
        mask: "lines",
        autoSplit: true,
        onSplit: (self) =>
          gsap
            .timeline()
            .from(self.words, { yPercent: 115, duration: 1.1, stagger: 0.04, ease: "power4.out" })
            .from(
              self.lines.flatMap((l) => Array.from(l.querySelectorAll(".hero-mark"))),
              { backgroundSize: "0% 0.38em", duration: 0.9, ease: "power3.inOut", stagger: 0.15 },
              "-=0.5",
            ),
      })
      const intro = gsap.timeline({ defaults: { ease: "power4.out" }, delay: 0.5 })
      intro
        .from(".hero-fade", { y: 18, autoAlpha: 0, duration: 0.8, stagger: 0.08 })
        .from(".float-card", { y: 60, autoAlpha: 0, rotate: 0, duration: 1.2, stagger: 0.12 }, "-=1")

      // Parallax: layers move at different speeds as the hero scrolls away.
      gsap.utils.toArray<HTMLElement>("[data-speed]").forEach((el) => {
        gsap.to(el, {
          yPercent: () => -Number(el.dataset.speed) * 100,
          ease: "none",
          scrollTrigger: { trigger: ".hero", start: "top top", end: "bottom top", scrub: true },
        })
      })
      gsap.to(".hero-grid", {
        backgroundPositionY: "120px",
        ease: "none",
        scrollTrigger: { trigger: ".hero", start: "top top", end: "bottom top", scrub: true },
      })

      // Pointer parallax on the floating cards (desktop pointers only).
      if (window.matchMedia("(pointer: fine)").matches) {
        const cards = gsap.utils.toArray<HTMLElement>(".float-card")
        const movers = cards.map((c) => ({
          x: gsap.quickTo(c, "x", { duration: 0.8, ease: "power3" }),
          y: gsap.quickTo(c, "y", { duration: 0.8, ease: "power3" }),
          depth: Number(c.dataset.depth ?? 1),
        }))
        const onMove = (e: PointerEvent) => {
          const dx = e.clientX / window.innerWidth - 0.5
          const dy = e.clientY / window.innerHeight - 0.5
          movers.forEach((m) => {
            m.x(dx * 28 * m.depth)
            m.y(dy * 20 * m.depth)
          })
        }
        window.addEventListener("pointermove", onMove)
        cleanup.push(() => window.removeEventListener("pointermove", onMove))
      }

      // Video frame grows into place.
      gsap.fromTo(
        ".video-frame",
        { scale: 0.86, y: 60, rotateX: 8 },
        {
          scale: 1,
          y: 0,
          rotateX: 0,
          ease: "none",
          scrollTrigger: { trigger: ".video-section", start: "top bottom", end: "center center", scrub: true },
        },
      )

      // Architecture: edges draw as the section scrolls; a packet runs the PR path.
      gsap.fromTo(
        ".arch-edge",
        { strokeDashoffset: 1, strokeDasharray: 1 },
        {
          strokeDashoffset: 0,
          ease: "none",
          stagger: 0.04,
          scrollTrigger: { trigger: ".how", start: "top 70%", end: "top 10%", scrub: true },
        },
      )
      gsap.from(".arch-node", {
        autoAlpha: 0,
        y: 14,
        stagger: 0.04,
        scrollTrigger: { trigger: ".how", start: "top 75%", end: "top 20%", scrub: true },
      })
      gsap.to(".arch-packet", {
        motionPath: { path: "#packet-path", align: "#packet-path", alignOrigin: [0.5, 0.5] },
        duration: 5.5,
        ease: "power1.inOut",
        repeat: -1,
        repeatDelay: 0.6,
      })

      // Section headings and cards rise in.
      gsap.utils.toArray<HTMLElement>(".reveal").forEach((el) => {
        gsap.from(el, {
          y: 40,
          autoAlpha: 0,
          duration: 0.9,
          ease: "power3.out",
          scrollTrigger: { trigger: el, start: "top 88%" },
        })
      })
      gsap.utils.toArray<HTMLElement>(".stagger").forEach((group) => {
        gsap.from(group.children, {
          y: 36,
          autoAlpha: 0,
          duration: 0.8,
          ease: "power3.out",
          stagger: 0.07,
          scrollTrigger: { trigger: group, start: "top 85%" },
        })
      })

      // Numbers count up from zero when they enter.
      gsap.utils.toArray<HTMLElement>("[data-count]").forEach((el) => {
        const target = Number(el.dataset.count)
        const decimals = Number(el.dataset.decimals ?? 0)
        const state = { v: 0 }
        gsap.to(state, {
          v: target,
          duration: 1.6,
          ease: "power2.out",
          scrollTrigger: { trigger: el, start: "top 90%" },
          onUpdate: () => {
            el.textContent = state.v.toLocaleString("en", {
              minimumFractionDigits: decimals,
              maximumFractionDigits: decimals,
            })
          },
        })
      })

      // The footer wordmark slides as the page ends.
      gsap.fromTo(
        ".wordmark",
        { xPercent: -8 },
        { xPercent: 4, ease: "none", scrollTrigger: { trigger: ".site-footer", start: "top bottom", end: "bottom bottom", scrub: true } },
      )
    }, root)
    return () => {
      cleanup.forEach((f) => f())
      ctx.revert()
    }
  }, [root])
}

// --- sections ------------------------------------------------------------------------

function Nav() {
  const [scrolled, setScrolled] = useState(false)
  useEffect(() => {
    const on = () => setScrolled(window.scrollY > 24)
    on()
    window.addEventListener("scroll", on, { passive: true })
    return () => window.removeEventListener("scroll", on)
  }, [])
  return (
    <header
      className={cn(
        "fixed inset-x-0 top-0 z-50 transition-[background-color,border-color,backdrop-filter] duration-300",
        scrolled ? "border-b-[1.5px] border-border/60 bg-background/80 backdrop-blur-md" : "border-b-[1.5px] border-transparent",
      )}
    >
      <nav className="mx-auto flex h-16 max-w-7xl items-center gap-6 px-5 sm:px-8" aria-label="Main">
        <Logo />
        <div className="ml-6 hidden items-center gap-6 font-mono text-[11px] uppercase tracking-[0.16em] text-muted-foreground md:flex">
          <a href="#how" className="transition-colors hover:text-foreground">How it works</a>
          <a href="#proof" className="transition-colors hover:text-foreground">Proof</a>
          <a href="#writeups" className="transition-colors hover:text-foreground">Write-ups</a>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Button asChild variant="ghost" size="icon-sm" className="hidden sm:inline-flex" aria-label="Argus on GitHub">
            <a href={GITHUB_URL} target="_blank" rel="noreferrer">
              <GitHubMark className="size-4" />
            </a>
          </Button>
          <ThemeToggle />
          <Button asChild variant="neon" size="sm" className="hidden sm:inline-flex">
            <a href="/demo">
              Live demo <ArrowRight />
            </a>
          </Button>
        </div>
      </nav>
    </header>
  )
}

function ReviewCard() {
  return (
    <div className="float-card panel w-[300px] p-4 text-left shadow-hard-lg" data-depth="1.2">
      <div className="mb-3 flex items-center gap-2">
        <span className="grid size-6 place-items-center rounded-sm bg-neon text-neon-foreground">
          <GitPullRequest className="size-3.5" />
        </span>
        <p className="truncate font-mono text-[11px]">checkout-service #1247</p>
        <span className="ml-auto rounded-sm border border-success/40 bg-success/10 px-1.5 font-mono text-[10px] uppercase text-success">
          reviewed
        </span>
      </div>
      <p className="mb-1 text-[13px] font-semibold">Missing idempotency key</p>
      <p className="font-mono text-[11px] text-muted-foreground">app/orders.py:12</p>
      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
        A retry after a gateway timeout can charge the customer twice. Derive the key from the
        order id.
      </p>
    </div>
  )
}

function TraceCard() {
  const hops = [
    ["webhook.receive", 2],
    ["outbox.relay", 6],
    ["queue.process", 10],
    ["agent.run", 86],
    ["llm.chat large", 58],
    ["POST comment", 9],
  ] as const
  return (
    <div className="float-card panel w-[280px] p-4 shadow-hard-lg" data-depth="0.7">
      <p className="eyebrow mb-3">One trace, every hop</p>
      <ul className="space-y-1.5">
        {hops.map(([name, w], i) => (
          <li key={name} className="flex items-center gap-2">
            <span className="w-28 shrink-0 truncate font-mono text-[10px] text-muted-foreground">{name}</span>
            <span className="h-2 rounded-sm bg-neon/80" style={{ width: `${w}%`, marginLeft: `${i * 4}%` }} />
          </li>
        ))}
      </ul>
    </div>
  )
}

function Hero() {
  return (
    <section className="hero relative isolate overflow-hidden pt-28 pb-20 sm:pt-36 lg:pb-28">
      <div
        aria-hidden
        className="hero-grid absolute inset-0 -z-10 opacity-60 [background-image:linear-gradient(var(--grid-line)_1px,transparent_1px),linear-gradient(90deg,var(--grid-line)_1px,transparent_1px)] [background-size:56px_56px] [mask-image:radial-gradient(ellipse_70%_60%_at_50%_30%,black,transparent)]"
      />
      <div
        aria-hidden
        data-speed="0.25"
        className="absolute -top-40 left-1/2 -z-10 size-[720px] -translate-x-1/2 rounded-full bg-[radial-gradient(circle,color-mix(in_srgb,var(--neon)_20%,transparent),transparent_65%)] blur-2xl"
      />
      <div className="mx-auto max-w-7xl px-5 sm:px-8">
        <p className="hero-fade eyebrow mb-6 inline-flex items-center gap-2 rounded-full border border-border/50 px-3 py-1">
          <span className="size-1.5 rounded-full bg-neon shadow-[0_0_8px_var(--neon)]" />
          AI code review · Codebase Q&amp;A
        </p>
        <h1 className="hero-title display max-w-[21ch] text-[clamp(2.1rem,6vw,5.25rem)] leading-[0.92] [overflow-wrap:anywhere] sm:[overflow-wrap:normal]">
          AI code review and codebase Q&amp;A for engineering teams,{" "}
          <span className="hero-mark">built to run at scale.</span>
        </h1>
        <div className="mt-10 grid items-start gap-12 lg:mt-14 lg:grid-cols-[1fr_1.1fr]">
          <div className="min-w-0">
            <p className="hero-fade max-w-xl text-lg text-muted-foreground">
              Argus reviews every pull request and answers questions about your code with citations,
              and keeps doing it through crashes, traffic floods and prompt injections.
            </p>
            <div className="hero-fade mt-8 flex flex-wrap items-center gap-3">
              <Button asChild variant="neon" size="lg" bracket>
                <a href="/demo">
                  Try live demo <ArrowRight />
                </a>
              </Button>
              <Button asChild variant="outline" size="lg">
                <a href={GITHUB_URL} target="_blank" rel="noreferrer">
                  <GitHubMark className="size-4" /> View on GitHub
                </a>
              </Button>
            </div>
            <p className="hero-fade mt-5 font-mono text-[11px] uppercase tracking-[0.16em] text-muted-foreground">
              No sign-up · read-only demo on sample data
            </p>
          </div>
          <div className="relative hidden h-[330px] lg:block" aria-hidden>
            <div data-speed="0.35" className="absolute top-0 left-4">
              <ReviewCard />
            </div>
            <div data-speed="0.6" className="absolute top-16 right-0">
              <TraceCard />
            </div>
            <div data-speed="0.9" className="absolute bottom-0 left-[38%]">
              <div className="float-card panel flex items-center gap-2 px-3 py-2 whitespace-nowrap shadow-hard" data-depth="1.6">
                <ShieldCheck className="size-4 text-success" />
                <span className="font-mono text-[11px]">injection flagged · tools reduced</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

const MARQUEE = [
  "Exactly-once PR comments",
  "Durable runs that survive kill -9",
  "Per-tenant fair scheduling",
  "Row-level security",
  "Prompt-injection guardrails",
  "LLM gateway with budgets",
  "One trace from webhook to comment",
  "Evals gate every merge",
]

function Marquee() {
  const items = [...MARQUEE, ...MARQUEE]
  return (
    <div className="relative overflow-hidden border-y-[1.5px] border-border bg-ink py-4 text-background" aria-label="Capabilities">
      <div className="marquee flex w-max gap-10 motion-reduce:animate-none">
        {items.map((t, i) => (
          <span key={i} className="flex items-center gap-10 font-mono text-xs uppercase tracking-[0.2em] whitespace-nowrap">
            {t}
            <span aria-hidden className="size-1.5 rotate-45 bg-neon" />
          </span>
        ))}
      </div>
    </div>
  )
}

function SectionHead({ eyebrow, title, children }: { eyebrow: string; title: React.ReactNode; children?: React.ReactNode }) {
  return (
    <div className="reveal mb-12 max-w-3xl">
      <p className="eyebrow mb-3">{eyebrow}</p>
      <h2 className="display text-[clamp(1.9rem,4vw,3.2rem)] leading-[0.95]">{title}</h2>
      {children && <p className="mt-5 text-base text-muted-foreground sm:text-lg">{children}</p>}
    </div>
  )
}

function VideoSlot() {
  const isEmbed = /youtube|youtu\.be|loom|vimeo/.test(DEMO_VIDEO_URL)
  return (
    <section className="video-section px-5 py-24 sm:px-8 sm:py-32" aria-labelledby="video-title" style={{ perspective: "1200px" }}>
      <div className="mx-auto max-w-6xl">
        <SectionHead eyebrow="Watch" title={<span id="video-title">Two minutes, one pull request</span>}>
          From a webhook to a cited review comment, then a question about the codebase.
        </SectionHead>
        <div className="video-frame panel overflow-hidden shadow-hard-lg" style={{ transformStyle: "preserve-3d" }}>
          <div className="flex items-center gap-1.5 border-b-[1.5px] border-border bg-muted px-4 py-2.5">
            <span className="size-2.5 rounded-full bg-destructive/70" />
            <span className="size-2.5 rounded-full bg-warning/70" />
            <span className="size-2.5 rounded-full bg-success/70" />
            <span className="ml-3 font-mono text-[11px] text-muted-foreground">argus · demo recording</span>
          </div>
          <div className="relative aspect-video bg-background">
            {DEMO_VIDEO_URL ? (
              isEmbed ? (
                <iframe
                  src={DEMO_VIDEO_URL}
                  title="Argus demo recording"
                  className="absolute inset-0 size-full"
                  allow="autoplay; fullscreen; picture-in-picture"
                  allowFullScreen
                />
              ) : (
                <video
                  className="absolute inset-0 size-full object-cover"
                  src={DEMO_VIDEO_URL}
                  poster={DEMO_VIDEO_POSTER || undefined}
                  controls
                  preload="metadata"
                />
              )
            ) : (
              <div className="absolute inset-0 grid place-items-center">
                <div
                  aria-hidden
                  className="absolute inset-0 opacity-40 [background-image:linear-gradient(var(--grid-line)_1px,transparent_1px),linear-gradient(90deg,var(--grid-line)_1px,transparent_1px)] [background-size:40px_40px]"
                />
                <div className="relative flex flex-col items-center gap-4 text-center">
                  <span className="grid size-16 place-items-center rounded-full border-[1.5px] border-border bg-neon text-neon-foreground shadow-hard">
                    <Play className="ml-1 size-6" />
                  </span>
                  <p className="display text-sm">Demo recording coming soon</p>
                  <p className="max-w-xs text-sm text-muted-foreground">
                    Meanwhile, click through the live demo: it is the real product on sample data.
                  </p>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  )
}

const CALLOUTS: { group: Group; icon: typeof Gauge; title: string; body: string }[] = [
  {
    group: "queue",
    icon: Gauge,
    title: "Every event lands in a durable queue",
    body:
      "Ingest only verifies the signature, dedupes the delivery and writes the event with an outbox row in one transaction, then returns 202. A relay moves it to Redis Streams, one lane per tenant, so a burst or a crash downstream never loses an event and one noisy team never starves the rest.",
  },
  {
    group: "durable",
    icon: CircleCheck,
    title: "Runs that survive crashes",
    body:
      "An agent run is a persisted state machine. Every LLM turn and tool call is checkpointed; a worker killed mid-run is replaced and the run resumes from its last step. Leases with fencing tokens stop a zombie worker from writing, and the comment is posted exactly once.",
  },
  {
    group: "gateway",
    icon: Sparkles,
    title: "One gateway for every model call",
    body:
      "Routing, prompt and semantic caching, token buckets per tenant, daily budgets, retries that honour Retry-After, and a circuit breaker that fails over to another model. Every call is accounted for with tokens and cost.",
  },
  {
    group: "tenancy",
    icon: ShieldCheck,
    title: "Tenant isolation in the database",
    body:
      "Postgres row-level security filters every query to the caller's organisation, even one that forgets its WHERE clause. Private repositories also need the user's own GitHub permission, and every decision is written to a hash-chained audit log.",
  },
  {
    group: "evals",
    icon: BookOpen,
    title: "Evals gate every merge",
    body:
      "Golden pull requests with planted bugs, a codebase Q&A set, an LLM judge checked against human labels and a prompt-injection suite run in CI. A worse prompt fails the build.",
  },
]

function HowItWorks() {
  const [active, setActive] = useState<Group | null>(null)
  const refs = useRef<(HTMLElement | null)[]>([])
  useEffect(() => {
    const io = new IntersectionObserver(
      (entries) => {
        const visible = entries.filter((e) => e.isIntersecting)
        if (visible.length) setActive((visible[0].target as HTMLElement).dataset.group as Group)
      },
      { rootMargin: "-45% 0px -45% 0px" },
    )
    refs.current.forEach((el) => el && io.observe(el))
    return () => io.disconnect()
  }, [])
  return (
    <section id="how" className="how scroll-mt-20 px-5 py-24 sm:px-8 sm:py-32" aria-labelledby="how-title">
      <div className="mx-auto max-w-7xl">
        <SectionHead eyebrow="How it works" title={<span id="how-title">Fourteen parts, one path</span>}>
          Every webhook passes through a durable queue before an agent touches it. Workers reach models
          and tools only through the gateway and MCP servers, where cost, limits and permissions are
          enforced.
        </SectionHead>
        <div className="grid gap-10 lg:grid-cols-[1.35fr_1fr] lg:gap-16">
          <div className="lg:sticky lg:top-24 lg:self-start">
            <div className="panel overflow-x-auto bg-card/60 p-3 sm:p-5" tabIndex={0} aria-label="Architecture diagram, scrolls sideways on small screens">
              <div className="min-w-[640px] sm:min-w-0">
                <ArchitectureDiagram active={active} />
              </div>
            </div>
            <p className="mt-3 font-mono text-[10px] uppercase tracking-wider text-muted-foreground sm:hidden">Swipe to see the whole diagram</p>
          </div>
          <ol className="space-y-6 lg:space-y-[28vh] lg:py-[12vh]">
            {CALLOUTS.map((c, i) => (
              <li
                key={c.group}
                ref={(el) => {
                  refs.current[i] = el
                }}
                data-group={c.group}
                onMouseEnter={() => setActive(c.group)}
                className={cn(
                  "panel p-5 transition-[border-color,box-shadow,transform] duration-300 sm:p-6",
                  active === c.group ? "border-neon shadow-[5px_5px_0_0_var(--neon)]" : "opacity-90",
                )}
              >
                <div className="mb-3 flex items-center gap-3">
                  <span className="num text-xs text-muted-foreground">0{i + 1}</span>
                  <span className={cn("grid size-8 place-items-center rounded-md border-[1.5px] border-border", active === c.group && "bg-neon text-neon-foreground")}>
                    <c.icon className="size-4" />
                  </span>
                  <h3 className="text-base font-semibold sm:text-lg">{c.title}</h3>
                </div>
                <p className="text-sm leading-relaxed text-muted-foreground">{c.body}</p>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </section>
  )
}

function Proof() {
  if (METRICS.length === 0) return null
  const sections = [
    { title: "Load and chaos", groups: ["Load"] },
    { title: "Quality and safety", groups: ["Evals", "Safety"] },
  ]
    .map((s) => ({ ...s, metrics: METRICS.filter((m) => s.groups.includes(m.group)) }))
    .filter((s) => s.metrics.length > 0)
  return (
    <section id="proof" className="scroll-mt-20 border-y-[1.5px] border-border bg-card/40 px-5 py-24 sm:px-8 sm:py-32" aria-labelledby="proof-title">
      <div className="mx-auto max-w-7xl">
        <SectionHead eyebrow="Proof in numbers" title={<span id="proof-title">Measured, not claimed</span>}>
          Every figure below is read at build time from a report in the repository. If a report does not
          exist yet, its numbers are not shown.
        </SectionHead>
        <div className="space-y-12">
          {sections.map((sec) => (
            <div key={sec.title}>
              <p className="eyebrow mb-4">{sec.title}</p>
              <div className="stagger grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {sec.metrics.map((m) => (
                  <a
                    key={m.id}
                    href={blob(m.source)}
                    target="_blank"
                    rel="noreferrer"
                    className="panel panel-interactive group flex flex-col p-5"
                  >
                    <p className="flex items-baseline gap-1 font-heading text-5xl font-extrabold leading-none tracking-tight">
                      <span className="num" data-count={m.value} data-decimals={m.decimals}>
                        {m.value.toLocaleString("en", { minimumFractionDigits: m.decimals, maximumFractionDigits: m.decimals })}
                      </span>
                      <span className="text-2xl text-muted-foreground">{m.unit}</span>
                    </p>
                    <p className="mt-4 font-semibold">{m.label}</p>
                    <p className="mt-1 text-sm text-muted-foreground">{m.detail}</p>
                    <p className="mt-auto flex items-center gap-1 pt-4 font-mono text-[10px] uppercase tracking-wider text-muted-foreground group-hover:text-foreground">
                      {m.source} <ArrowUpRight className="size-3" />
                    </p>
                  </a>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}

function Writeups() {
  return (
    <section id="writeups" className="scroll-mt-20 px-5 py-24 sm:px-8 sm:py-32" aria-labelledby="writeups-title">
      <div className="mx-auto max-w-7xl">
        <SectionHead eyebrow="Engineering write-ups" title={<span id="writeups-title">The decisions, written down</span>}>
          Architecture decision records for every significant choice, the load and chaos report with the
          failures it found, and the eval results that gate each change.
        </SectionHead>
        {REPORTS.length > 0 && (
          <div className="stagger mb-12 grid gap-5 md:grid-cols-3">
            {REPORTS.map((r) => (
              <a key={r.path} href={blob(r.path)} target="_blank" rel="noreferrer" className="panel panel-interactive group flex flex-col !bg-[#141414] p-6 text-[#f5f5f5]">
                <FileText className="size-5 text-neon" />
                <p className="mt-6 text-lg font-semibold">{r.title}</p>
                <p className="mt-2 text-sm text-[#f5f5f5]/70">{r.body}</p>
                <p className="mt-auto flex items-center gap-1 pt-6 font-mono text-[10px] uppercase tracking-wider text-neon">
                  Read the report <ArrowUpRight className="size-3 transition-transform group-hover:-translate-y-0.5 group-hover:translate-x-0.5" />
                </p>
              </a>
            ))}
          </div>
        )}
        <div className="stagger grid gap-3 md:grid-cols-2">
          {ADRS.map((a) => (
            <a key={a.number} href={blob(a.path)} target="_blank" rel="noreferrer" className="group flex items-start gap-4 rounded-md border-[1.5px] border-border/25 bg-background px-5 py-4 transition-colors hover:border-border hover:bg-card">
              <span className="num shrink-0 pt-0.5 text-xs text-muted-foreground">ADR {a.number}</span>
              <span className="flex-1 text-sm font-medium leading-snug">{a.title}</span>
              <ArrowUpRight className="size-4 shrink-0 text-muted-foreground transition-transform group-hover:-translate-y-0.5 group-hover:translate-x-0.5 group-hover:text-foreground" />
            </a>
          ))}
        </div>
        <p className="mt-6 text-sm text-muted-foreground">
          All records:{" "}
          <a href={tree("docs/adr")} target="_blank" rel="noreferrer" className="underline decoration-neon decoration-2 underline-offset-4">
            docs/adr
          </a>
        </p>
      </div>
    </section>
  )
}

function Cta() {
  return (
    <section className="px-5 pb-24 sm:px-8 sm:pb-32">
      <div className="reveal relative mx-auto max-w-7xl overflow-hidden rounded-md border-[1.5px] border-border bg-neon p-8 text-[#1b1b1b] shadow-hard-lg sm:p-14">
        <div aria-hidden className="absolute -right-24 -bottom-24 size-80 rounded-full border-[40px] border-black/10" />
        <p className="font-mono text-[11px] font-bold uppercase tracking-[0.2em]">Live demo</p>
        <h2 className="display mt-3 max-w-3xl text-[clamp(1.9rem,4.4vw,3.6rem)] leading-[0.95]">
          A month of a real-shaped team, one click away.
        </h2>
        <p className="mt-4 max-w-xl text-base opacity-80">
          Four repositories, hundreds of reviews, approvals waiting, questions with cited answers. Read-only,
          no sign-up.
        </p>
        <div className="mt-8 flex flex-wrap gap-3">
          <a
            href="/demo"
            className="inline-flex h-11 items-center gap-2 rounded-md border-[1.5px] border-[#1b1b1b] bg-[#1b1b1b] px-5 text-sm font-semibold uppercase tracking-wider text-neon shadow-[4px_4px_0_0_rgba(0,0,0,0.25)] transition-transform hover:-translate-y-0.5"
          >
            Open the demo <ArrowRight className="size-4" />
          </a>
          <a
            href={GITHUB_URL}
            target="_blank"
            rel="noreferrer"
            className="inline-flex h-11 items-center gap-2 rounded-md border-[1.5px] border-[#1b1b1b] px-5 text-sm font-semibold uppercase tracking-wider transition-colors hover:bg-black/10"
          >
            <GitHubMark className="size-4" /> Source
          </a>
        </div>
      </div>
    </section>
  )
}

function Footer() {
  return (
    <footer className="site-footer overflow-hidden border-t-[1.5px] border-border px-5 pt-16 pb-8 sm:px-8">
      <div className="mx-auto grid max-w-7xl gap-10 sm:grid-cols-[1.5fr_1fr_1fr]">
        <div>
          <Logo />
          <p className="mt-4 max-w-sm text-sm text-muted-foreground">
            AI code review and codebase Q&amp;A for engineering teams, built to run at scale.
          </p>
        </div>
        <div>
          <p className="eyebrow mb-3">Product</p>
          <ul className="space-y-2 text-sm">
            <li><a className="hover:underline" href="/demo">Live demo</a></li>
            <li><a className="hover:underline" href="#how">How it works</a></li>
            <li><a className="hover:underline" href="#proof">Proof</a></li>
          </ul>
        </div>
        <div>
          <p className="eyebrow mb-3">Built by</p>
          <p className="text-sm font-semibold">{AUTHOR}</p>
          <ul className="mt-2 space-y-2 text-sm">
            <li>
              <a className="inline-flex items-center gap-1.5 hover:underline" href={GITHUB_URL} target="_blank" rel="noreferrer">
                <GitHubMark className="size-3.5" /> GitHub
              </a>
            </li>
            <li>
              {LINKEDIN_URL ? (
                <a className="hover:underline" href={LINKEDIN_URL} target="_blank" rel="noreferrer">LinkedIn</a>
              ) : (
                <span className="text-muted-foreground" title="LinkedIn link coming soon">LinkedIn (coming soon)</span>
              )}
            </li>
          </ul>
        </div>
      </div>
      <p aria-hidden className="wordmark display mt-16 text-[22vw] leading-[0.8] tracking-tighter whitespace-nowrap text-foreground/[0.06] select-none">
        ARGUS · ARGUS
      </p>
      <p className="mx-auto mt-4 max-w-7xl font-mono text-[11px] text-muted-foreground">
        © {YEAR} {AUTHOR}
      </p>
    </footer>
  )
}

export function Landing() {
  const root = useRef<HTMLElement>(null)
  useSmoothScroll()
  useLandingMotion(root)
  return (
    <>
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-[60] focus:rounded-md focus:bg-neon focus:px-3 focus:py-2 focus:text-neon-foreground">
        Skip to content
      </a>
      <Nav />
      <main id="main" ref={root}>
        <Hero />
        <Marquee />
        <VideoSlot />
        <HowItWorks />
        <Proof />
        <Writeups />
        <Cta />
      </main>
      <Footer />
    </>
  )
}
