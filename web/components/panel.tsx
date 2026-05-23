import { cn } from "@/lib/utils";

interface PanelProps {
  title: string;
  subtitle?: string;
  hotkey?: string;
  className?: string;
  bodyClassName?: string;
  children: React.ReactNode;
  rightSlot?: React.ReactNode;
}

export function Panel({
  title,
  subtitle,
  hotkey,
  className,
  bodyClassName,
  children,
  rightSlot,
}: PanelProps) {
  return (
    <section
      className={cn(
        "flex flex-col overflow-hidden rounded-md border border-terminal-border bg-terminal-surface",
        className,
      )}
    >
      <header className="flex items-center justify-between border-b border-terminal-border bg-terminal-surfaceAlt px-3 py-1.5">
        <div className="flex min-w-0 items-center gap-2">
          {hotkey && (
            <span className="rounded border border-terminal-border bg-terminal-bg px-1 py-px font-mono text-[10px] uppercase text-terminal-dim">
              {hotkey}
            </span>
          )}
          <h2 className="truncate font-mono text-[11px] font-semibold uppercase tracking-wider text-terminal-textBright">
            {title}
          </h2>
          {subtitle && (
            <span className="truncate font-mono text-[10px] uppercase text-terminal-dim">
              {subtitle}
            </span>
          )}
        </div>
        {rightSlot && <div className="flex items-center gap-2">{rightSlot}</div>}
      </header>
      <div className={cn("min-h-0 flex-1 overflow-auto", bodyClassName)}>{children}</div>
    </section>
  );
}
