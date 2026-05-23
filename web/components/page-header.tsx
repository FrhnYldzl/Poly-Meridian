import { cn } from "@/lib/utils";

interface PageHeaderProps {
  title: string;
  subtitle?: string;
  badge?: string;
  className?: string;
  rightSlot?: React.ReactNode;
}

export function PageHeader({ title, subtitle, badge, className, rightSlot }: PageHeaderProps) {
  return (
    <div
      className={cn(
        "flex items-end justify-between border-b border-terminal-border bg-terminal-surface px-4 py-3",
        className,
      )}
    >
      <div className="flex items-end gap-3">
        <div>
          <h1 className="font-mono text-base font-semibold uppercase tracking-wider text-terminal-textBright">
            {title}
          </h1>
          {subtitle && (
            <p className="mt-0.5 font-mono text-[11px] uppercase tracking-wider text-terminal-dim">
              {subtitle}
            </p>
          )}
        </div>
        {badge && (
          <span className="rounded border border-terminal-amber/40 bg-terminal-amber/10 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider text-terminal-amber">
            {badge}
          </span>
        )}
      </div>
      {rightSlot}
    </div>
  );
}

export function ComingSoon({ phase = "Phase 7b" }: { phase?: string }) {
  return (
    <div className="grid-bg flex h-full min-h-[400px] flex-col items-center justify-center gap-2 px-6 py-10 text-center">
      <span className="font-mono text-[10px] uppercase tracking-[0.3em] text-terminal-amber">
        Coming in {phase}
      </span>
      <h2 className="font-mono text-xl uppercase tracking-wider text-terminal-textBright">
        This view is scaffolded
      </h2>
      <p className="max-w-md font-mono text-[11px] leading-relaxed text-terminal-dim">
        The data model and routes are wired. Real-time visualizations land in the
        next iteration once the backend has streamed enough paper-trade history.
      </p>
    </div>
  );
}
