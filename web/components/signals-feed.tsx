"use client";

import { Panel } from "./panel";
import { cn, formatPct, relativeTime } from "@/lib/utils";
import type { Signal } from "@/lib/types";

interface SignalsFeedProps {
  signals: Signal[];
}

export function SignalsFeed({ signals }: SignalsFeedProps) {
  return (
    <Panel title="Strategy signals" subtitle={`${signals.length} recent`} hotkey="2" bodyClassName="font-mono text-[11px]">
      {signals.length === 0 ? (
        <div className="flex h-full items-center justify-center text-terminal-dim">
          waiting for signals…
        </div>
      ) : (
        <ul className="divide-y divide-terminal-border/60">
          {signals.map((s, i) => (
            <SignalRow key={`${s.ts}-${s.condition_id}-${i}`} s={s} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

function SignalRow({ s }: { s: Signal }) {
  const actionTone =
    s.suggested_action === "BUY_YES"
      ? "text-terminal-green"
      : s.suggested_action === "BUY_NO"
        ? "text-terminal-red"
        : "text-terminal-dim";
  const stratBase = s.strategy.split(".")[0];
  const stratColor =
    {
      arbitrage: "text-terminal-cyan",
      sentiment: "text-terminal-purple",
      smart_money: "text-terminal-amber",
      stat_quant: "text-terminal-yellow",
      fundamentals: "text-terminal-green",
    }[stratBase as keyof object] ?? "text-terminal-text";
  // Compact rationale: pull first 3 numeric/short fields for the tooltip.
  const rationaleStr = s.rationale
    ? Object.entries(s.rationale)
        .slice(0, 5)
        .map(([k, v]) => `${k}=${typeof v === "number" ? v.toFixed(3) : String(v).slice(0, 24)}`)
        .join(" · ")
    : "";
  const tipBody = [
    s.market_question ? `market: ${s.market_question}` : null,
    `condition: ${s.condition_id}`,
    `conviction: ${(s.conviction ?? 0).toFixed(2)}`,
    rationaleStr ? `why: ${rationaleStr}` : null,
  ]
    .filter(Boolean)
    .join("\n");

  return (
    <li
      className="grid grid-cols-[72px_120px_56px_1fr_56px] items-center gap-3 px-3 py-1.5 hover:bg-terminal-surfaceAlt/50"
      title={tipBody}
    >
      <span className="text-terminal-dim">{relativeTime(s.ts)}</span>
      <span className={cn("truncate font-semibold uppercase tracking-wider", stratColor)}>
        {s.strategy}
      </span>
      <span className={cn("font-semibold uppercase", actionTone)}>
        {s.suggested_action.replace("_", " ")}
      </span>
      <span className="truncate text-terminal-text">
        {s.market_question ?? `${s.condition_id.slice(0, 18)}…`}
      </span>
      <span className="numeric text-right text-terminal-amber">
        {formatPct(s.edge, { showSign: true, precision: 1 })}
      </span>
    </li>
  );
}
