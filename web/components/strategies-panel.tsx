"use client";

import { Panel } from "./panel";
import { cn } from "@/lib/utils";
import type { Signal } from "@/lib/types";

interface StrategiesPanelProps {
  enabled: string[];
  signals: Signal[];
}

const STRATEGY_COLORS: Record<string, string> = {
  arbitrage: "bg-terminal-cyan/20 text-terminal-cyan border-terminal-cyan/40",
  sentiment: "bg-terminal-purple/20 text-terminal-purple border-terminal-purple/40",
  smart_money: "bg-terminal-amber/20 text-terminal-amber border-terminal-amber/40",
  stat_quant: "bg-terminal-yellow/20 text-terminal-yellow border-terminal-yellow/40",
  fundamentals: "bg-terminal-green/20 text-terminal-green border-terminal-green/40",
};

const STRATEGY_LABELS: Record<string, string> = {
  arbitrage: "Arbitrage",
  sentiment: "Sentiment",
  smart_money: "Smart Money",
  stat_quant: "Stat Quant",
  fundamentals: "Fundamentals",
};

export function StrategiesPanel({ enabled, signals }: StrategiesPanelProps) {
  // Bucket signals per strategy in the last window (max 50 entries).
  // StatQuant emits as "stat_quant.mean_reversion" / ".momentum" / etc —
  // match by the leading base name so all variants roll up to one bucket.
  const counts = new Map<string, number>();
  for (const s of signals) {
    const base = (s.strategy ?? "").split(".")[0];
    if (!base) continue;
    counts.set(base, (counts.get(base) ?? 0) + 1);
  }
  const total = signals.length;

  return (
    <Panel
      title="Strategies"
      subtitle={`${enabled.length} enabled`}
      hotkey="5"
      bodyClassName="font-mono text-[11px]"
    >
      <div className="grid grid-cols-2 gap-2 p-3 sm:grid-cols-3 xl:grid-cols-5">
        {(["arbitrage", "sentiment", "smart_money", "stat_quant", "fundamentals"] as const).map(
          (name) => {
            const isOn = enabled.includes(name);
            const count = counts.get(name) ?? 0;
            const share = total > 0 ? count / total : 0;
            return (
              <div
                key={name}
                className={cn(
                  "flex flex-col rounded border bg-terminal-bg p-2",
                  isOn ? STRATEGY_COLORS[name] : "border-terminal-border text-terminal-dim",
                )}
              >
                <span className="text-[10px] uppercase tracking-wider">{STRATEGY_LABELS[name]}</span>
                <span className="numeric mt-1 text-2xl font-semibold">{count}</span>
                <span className="text-[10px] text-terminal-dim">
                  {isOn ? `${(share * 100).toFixed(0)}% of signals` : "disabled"}
                </span>
                <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-terminal-border/40">
                  <div
                    className={cn(
                      "h-full transition-all",
                      isOn ? "bg-current" : "bg-terminal-border",
                    )}
                    style={{ width: `${Math.min(100, share * 100)}%` }}
                  />
                </div>
              </div>
            );
          },
        )}
      </div>
    </Panel>
  );
}
