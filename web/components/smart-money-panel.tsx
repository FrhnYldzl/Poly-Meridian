"use client";

import { Panel } from "./panel";
import { cn, formatUsd, relativeTime } from "@/lib/utils";
import type { SmartMoneyCluster } from "@/lib/types";

interface SmartMoneyPanelProps {
  clusters: SmartMoneyCluster[];
}

export function SmartMoneyPanel({ clusters }: SmartMoneyPanelProps) {
  return (
    <Panel
      title="Smart money clusters"
      subtitle={`${clusters.length} active`}
      hotkey="4"
      bodyClassName="font-mono text-[11px]"
    >
      {clusters.length === 0 ? (
        <div className="flex h-full flex-col items-center justify-center gap-2 px-4 py-3 text-center text-terminal-dim">
          <span className="text-[28px] leading-none text-terminal-dim/40">🦈</span>
          <span className="text-[11px]">no active whale clusters</span>
          <span className="text-[10px] leading-snug text-terminal-dim/70">
            fires when ≥3 Tier-1 wallets buy the same outcome within 30 min
          </span>
          <div className="mt-2 grid grid-cols-3 gap-1 text-[9px] uppercase tracking-wider">
            <span className="rounded border border-terminal-green/30 px-1 py-0.5 text-terminal-green/70">T1 · proven</span>
            <span className="rounded border border-terminal-yellow/30 px-1 py-0.5 text-terminal-yellow/70">T2 · hot</span>
            <span className="rounded border border-terminal-cyan/30 px-1 py-0.5 text-terminal-cyan/70">T3 · watch</span>
          </div>
        </div>
      ) : (
        <ul className="divide-y divide-terminal-border/60">
          {clusters.map((c, i) => (
            <ClusterRow key={`${c.condition_id}-${i}`} c={c} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

function ClusterRow({ c }: { c: SmartMoneyCluster }) {
  const tierTone = c.tier === 1 ? "text-terminal-green" : c.tier === 2 ? "text-terminal-yellow" : "text-terminal-cyan";
  const dirTone = c.direction === "YES" ? "text-terminal-green" : "text-terminal-red";
  return (
    <li className="grid grid-cols-[40px_50px_60px_1fr_90px_60px] items-center gap-3 px-3 py-1.5 hover:bg-terminal-surfaceAlt/50">
      <span className={cn("font-semibold", tierTone)}>T{c.tier}</span>
      <span className={cn("font-semibold uppercase", dirTone)}>{c.direction}</span>
      <span className="text-terminal-amber numeric">{c.cluster_size}w</span>
      <span className="truncate text-terminal-text">{c.condition_id.slice(0, 18)}…</span>
      <span className="numeric text-right text-terminal-textBright">{formatUsd(c.net_usd_total)}</span>
      <span className="text-right text-terminal-dim">{relativeTime(c.ts)}</span>
    </li>
  );
}
