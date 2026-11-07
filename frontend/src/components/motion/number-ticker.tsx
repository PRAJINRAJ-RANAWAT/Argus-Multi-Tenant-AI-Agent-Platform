import { useEffect, useRef } from "react"
import { useInView, useMotionValue, useReducedMotion, useSpring } from "motion/react"

import { cn } from "@/lib/utils"

/** Counts up to `value` when scrolled into view (after Magic UI's NumberTicker).
 *  Renders the final value immediately for reduced-motion users. */
export function NumberTicker({
  value,
  format = (n) => Math.round(n).toLocaleString(),
  className,
}: {
  value: number
  format?: (n: number) => string
  className?: string
}) {
  const ref = useRef<HTMLSpanElement>(null)
  const reduce = useReducedMotion()
  const motionValue = useMotionValue(reduce ? value : 0)
  const spring = useSpring(motionValue, { damping: 40, stiffness: 180 })
  const inView = useInView(ref, { once: true, margin: "0px" })

  useEffect(() => {
    if (reduce) {
      if (ref.current) ref.current.textContent = format(value)
      return
    }
    if (inView) motionValue.set(value)
  }, [inView, value, reduce, motionValue, format])

  useEffect(() => {
    if (reduce) return
    return spring.on("change", (v) => {
      if (ref.current) ref.current.textContent = format(v)
    })
  }, [spring, format, reduce])

  return (
    <span ref={ref} className={cn("num", className)}>
      {format(reduce ? value : 0)}
    </span>
  )
}
