import { cn } from "@/lib/utils"

/** A neon beam that runs around its parent's border (after Magic UI's BorderBeam).
 *  The parent must be `relative`. Hidden for reduced-motion users. */
export function BorderBeam({ className, duration = 6 }: { className?: string; duration?: number }) {
  return (
    <span
      aria-hidden
      className={cn(
        "pointer-events-none absolute -inset-[1.5px] rounded-[inherit] motion-reduce:hidden",
        "[mask:linear-gradient(#000_0_0)_content-box_exclude,linear-gradient(#000_0_0)] p-[1.5px]",
        className,
      )}
    >
      <span
        className="absolute inset-[-100%] animate-spin"
        style={{
          animationDuration: `${duration}s`,
          background:
            "conic-gradient(from 0deg, transparent 0deg, transparent 300deg, var(--neon) 345deg, transparent 360deg)",
        }}
      />
    </span>
  )
}
