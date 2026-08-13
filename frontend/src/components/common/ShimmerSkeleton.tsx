import { cn } from "../../lib/utils"

interface SkeletonProps extends React.HTMLAttributes<HTMLDivElement> {
  className?: string
}

export function ShimmerSkeleton({ className, ...props }: SkeletonProps) {
  return (
    <div
      className={cn("rounded-md bg-muted/60 animate-shimmer overflow-hidden", className)}
      {...props}
    />
  )
}

export function WorkspaceCardSkeleton() {
  return (
    <div className="h-64 rounded-2xl border border-border/70 p-6 flex flex-col justify-between bg-card/60">
      <div className="space-y-4">
        <ShimmerSkeleton className="h-10 w-10 rounded-lg" />
        <ShimmerSkeleton className="h-6 w-3/4 rounded-md" />
        <div className="space-y-2">
          <ShimmerSkeleton className="h-4 w-full rounded" />
          <ShimmerSkeleton className="h-4 w-5/6 rounded" />
        </div>
      </div>
      <div className="pt-4 border-t border-border/40 flex items-center justify-between">
        <ShimmerSkeleton className="h-4 w-20 rounded" />
        <ShimmerSkeleton className="h-6 w-24 rounded-full" />
      </div>
    </div>
  )
}

export function TableRowSkeleton() {
  return (
    <div className="flex items-center justify-between py-4 px-6 border-b border-border/40">
      <div className="flex items-center gap-3 w-1/3">
        <ShimmerSkeleton className="h-5 w-5 rounded" />
        <ShimmerSkeleton className="h-4 w-48 rounded" />
      </div>
      <ShimmerSkeleton className="h-6 w-14 rounded-md" />
      <ShimmerSkeleton className="h-4 w-24 rounded" />
      <ShimmerSkeleton className="h-4 w-12 rounded" />
      <ShimmerSkeleton className="h-6 w-20 rounded-full" />
    </div>
  )
}
