import { cn } from "@/lib/utils";

type Tone = "ok" | "warn" | "alert" | "neutral" | "info";

interface StatusPillProps {
  tone: Tone;
  label: string;
  value?: string;
  className?: string;
  pulse?: boolean;
}

const toneStyles: Record<Tone, string> = {
  ok: "text-terminal-green border-terminal-green/40 bg-terminal-green/10",
  warn: "text-terminal-yellow border-terminal-yellow/40 bg-terminal-yellow/10",
  alert: "text-terminal-red border-terminal-red/40 bg-terminal-red/10",
  info: "text-terminal-cyan border-terminal-cyan/40 bg-terminal-cyan/10",
  neutral: "text-terminal-dim border-terminal-border bg-terminal-bg",
};

export function StatusPill({ tone, label, value, className, pulse }: StatusPillProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded border px-2 py-0.5 font-mono text-[11px] uppercase tracking-wider",
        toneStyles[tone],
        className,
      )}
    >
      <span
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          tone === "ok" && "bg-terminal-green",
          tone === "warn" && "bg-terminal-yellow",
          tone === "alert" && "bg-terminal-red animate-blink",
          tone === "info" && "bg-terminal-cyan",
          tone === "neutral" && "bg-terminal-dim",
          pulse && tone !== "alert" && "animate-pulse-slow",
        )}
      />
      <span className="font-semibold">{label}</span>
      {value && <span className="numeric ml-1 font-normal">{value}</span>}
    </span>
  );
}
