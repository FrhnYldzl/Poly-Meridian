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

  // Phase R.9 — LLM "why" surfacing. When the resolver was LLM-driven
  // (fundamentals path), pull the structured rationale fields the
  // prompt requested: rationale text + base_rate + key_factors.
  const r = (s.rationale ?? {}) as Record<string, unknown>;
  const isLLM = stratBase === "fundamentals" && (
    typeof r.rationale === "string" || Array.isArray(r.key_factors)
  );
  const llmReason = typeof r.rationale === "string" ? (r.rationale as string) : null;
  const llmFactors = Array.isArray(r.key_factors)
    ? (r.key_factors as string[]).slice(0, 3).join(" · ")
    : null;
  const llmBase = typeof r.base_rate === "number" ? (r.base_rate as number) : null;
  const ourP = typeof r.our_p_long === "number" ? (r.our_p_long as number) : null;
  const mktP = typeof r.market_p_long === "number" ? (r.market_p_long as number) : null;

  // Tooltip lines, prioritized for LLM signals.
  const tipLines: string[] = [];
  if (s.market_question) tipLines.push(`market: ${s.market_question}`);
  tipLines.push(`condition: ${s.condition_id}`);
  tipLines.push(`conviction: ${(s.conviction ?? 0).toFixed(2)}`);
  if (isLLM) {
    if (ourP !== null && mktP !== null) {
      tipLines.push(`our p_long: ${ourP.toFixed(3)} · market p_long: ${mktP.toFixed(3)}`);
    }
    if (llmBase !== null) tipLines.push(`base_rate: ${llmBase.toFixed(3)}`);
    if (llmReason) tipLines.push(`Claude: ${llmReason}`);
    if (llmFactors) tipLines.push(`factors: ${llmFactors}`);
  } else {
    const rationaleStr = Object.entries(r)
      .slice(0, 5)
      .map(([k, v]) => `${k}=${typeof v === "number" ? v.toFixed(3) : String(v).slice(0, 24)}`)
      .join(" · ");
    if (rationaleStr) tipLines.push(`why: ${rationaleStr}`);
  }
  const tipBody = tipLines.join("\n");

  return (
    <li
      className="border-l-2 border-transparent px-3 py-1.5 hover:bg-terminal-surfaceAlt/50"
      title={tipBody}
      style={isLLM ? { borderLeftColor: "rgb(74, 222, 128)" } : undefined}
    >
      <div className="grid grid-cols-[72px_120px_56px_1fr_56px] items-center gap-3">
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
      </div>
      {/* Phase R.9 — inline 1-line rationale for LLM signals. Compresses
          Claude's reasoning to ~80 chars so the operator can audit
          without hovering. Full detail still in tooltip. */}
      {isLLM && (llmReason || llmFactors) && (
        <div className="mt-0.5 ml-[72px] truncate text-[10px] text-terminal-dim">
          {llmReason ? (
            <span>
              <span className="text-terminal-green">Claude:</span> {llmReason}
            </span>
          ) : (
            <span className="text-terminal-dim">{llmFactors}</span>
          )}
        </div>
      )}
    </li>
  );
}
