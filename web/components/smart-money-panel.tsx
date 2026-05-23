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
        <div className="flex h-full flex-col items-center justify-center gap-1 text-terminal-dim">
          <span>no active clusters</span>
          <span className="text-[10px] text-terminal-dim/70">
            waiting for ≥3 Tier 1 wallets same direction
          </span>
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
