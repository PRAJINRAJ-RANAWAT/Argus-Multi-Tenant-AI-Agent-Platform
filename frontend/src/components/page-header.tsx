import { BlurFade } from "@/components/motion/blur-fade"

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string
  title: string
  description?: React.ReactNode
  actions?: React.ReactNode
}) {
  return (
    <BlurFade className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        <p className="eyebrow mb-1">{eyebrow}</p>
        <h1 className="display text-2xl leading-none sm:text-3xl">{title}</h1>
        {description && (
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">{description}</p>
        )}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </BlurFade>
  )
}
