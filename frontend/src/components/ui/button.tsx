import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"
import { Slot } from "radix-ui"

const buttonVariants = cva(
  "group/button inline-flex shrink-0 items-center justify-center rounded-md border-[1.5px] border-transparent font-sans text-xs font-semibold uppercase tracking-wider whitespace-nowrap outline-none select-none transition-[transform,box-shadow,background-color,color] duration-150 ease-out focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-50 aria-invalid:border-destructive [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        // Ink fill with a hard offset shadow: lifts on hover, presses on click.
        default:
          "border-border bg-primary text-primary-foreground shadow-hard-sm hover:-translate-x-px hover:-translate-y-px hover:shadow-hard active:translate-x-px active:translate-y-px active:shadow-none",
        neon:
          "border-ink bg-neon text-neon-foreground shadow-hard-sm hover:-translate-x-px hover:-translate-y-px hover:shadow-hard active:translate-x-px active:translate-y-px active:shadow-none",
        outline:
          "border-border bg-card text-foreground shadow-hard-sm hover:-translate-x-px hover:-translate-y-px hover:shadow-hard active:translate-x-px active:translate-y-px active:shadow-none aria-expanded:bg-muted",
        secondary: "bg-secondary text-secondary-foreground hover:bg-muted-foreground/15",
        ghost: "normal-case tracking-normal hover:bg-muted aria-expanded:bg-muted",
        destructive:
          "border-destructive bg-destructive text-white shadow-[3px_3px_0_0_var(--shadow-ink)] hover:-translate-x-px hover:-translate-y-px active:translate-x-px active:translate-y-px active:shadow-none",
        link: "normal-case tracking-normal text-foreground underline underline-offset-4 decoration-neon decoration-2 hover:decoration-foreground",
      },
      size: {
        default: "h-9 gap-2 px-3.5",
        xs: "h-6 gap-1 px-2 text-[10px] [&_svg:not([class*='size-'])]:size-3",
        sm: "h-8 gap-1.5 px-2.5 text-[11px] [&_svg:not([class*='size-'])]:size-3.5",
        lg: "h-11 gap-2 px-5 text-sm",
        icon: "size-9",
        "icon-xs": "size-6 [&_svg:not([class*='size-'])]:size-3",
        "icon-sm": "size-8",
        "icon-lg": "size-10",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant = "default",
  size = "default",
  asChild = false,
  bracket = false,
  children,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean
    /** Render the label as "[ LABEL ]", the reference site's call-to-action style. */
    bracket?: boolean
  }) {
  const Comp = asChild ? Slot.Root : "button"

  return (
    <Comp
      data-slot="button"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    >
      {bracket && !asChild ? (
        <>
          <span aria-hidden className="opacity-60">[</span>
          {children}
          <span aria-hidden className="opacity-60">]</span>
        </>
      ) : (
        children
      )}
    </Comp>
  )
}

export { Button, buttonVariants }
