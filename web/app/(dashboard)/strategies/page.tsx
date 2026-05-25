"use client";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/page-header";
import { Panel } from "@/components/panel";
import { StrategiesPanel } from "@/components/strategies-panel";
import { useSharedAgentState } from "@/hooks/use-shared-agent-state";
import { cn, formatUsd, relativeTime } from "@/lib/utils";

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

interface StrategyPnlRow {
  strategy: string;
  fill_count: number;
  realized_pnl: number | null;
  fees: number | null;
  gross_notional: number | null;
  win_count: number;
}

const STRATEGY_DETAIL: Record<string, { description: string; section: string; tone: string }> = {
  arbitrage: {
    description:
      "Single-market complete-set arb. Detects YES_ask + NO_ask < 1 (after worst-case fees). Tight cluster confirmation, maker-first.",
    section: "§14.1",
    tone: "border-terminal-cyan/40 bg-terminal-cyan/10 text-terminal-cyan",
  },
  sentiment: {
    description:
      "News-driven directional bets. OpenAI embeddings + Claude scorer; impact-weighted sentiment shift on book mid.",
    section: "§14.2",
    tone: "border-terminal-purple/40 bg-terminal-purple/10 text-terminal-purple",
  },
  smart_money: {
    description:
      "3-tier follow-on of top Polymarket wallets. Latency decay 30min, per-trader cap 5%, drawdown filter, attribution log.",
    section: "§14.3",
    tone: "border-terminal-amber/40 bg-terminal-amber/10 text-terminal-amber",
  },
  stat_quant: {
    description:
      "Four sub-signals: mean reversion (z-score), momentum (lookback + volume gate), vol breakout, time decay <24h.",
    section: "§14.4",
    tone: "border-terminal-yellow/40 bg-terminal-yellow/10 text-terminal-yellow",
  },
  fundamentals: {
    description:
      "Category-specific probability models: Politics (poll aggregator), Sports (Elo), Crypto (TA + funding + netflow), Macro (calendar).",
    section: "§14.5",
    tone: "border-terminal-green/40 bg-terminal-green/10 text-terminal-green",
  },
};

interface PerStratStats {
  signals: number;
  shareOfSignals: number;
  lastSeen: string | null;
  lastEdgeBps: number | null;
}

export default function StrategiesPage() {
  const { snapshot } = useSharedAgentState();
  const enabled = snapshot?.strategies_enabled ?? [];
  const signals = snapshot?.last_signals ?? [];

  // Per-strategy PNL attribution — fetched from /api/strategy-pnl every
  // 30s. ledger_entries-driven so it survives restarts.
  const [pnlRows, setPnlRows] = useState<StrategyPnlRow[]>([]);
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const r = await fetch(`${API}/api/strategy-pnl?days=30`);
        const j = await r.json();
        if (!cancelled && Array.isArray(j.rows)) setPnlRows(j.rows);
      } catch {
        /* ignore */
      }
    }
    load();
    const id = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const pnlByStrategy: Record<string, StrategyPnlRow> = {};
  for (const r of pnlRows) {
    const base = (r.strategy || "").split(".")[0];
    if (!base) continue;
    // Multiple sub-strategies under same base — sum into one bucket.
    const existing = pnlByStrategy[base];
    if (!existing) {
      pnlByStrategy[base] = { ...r, strategy: base };
    } else {
      existing.fill_count += r.fill_count;
      existing.realized_pnl = (existing.realized_pnl ?? 0) + (r.realized_pnl ?? 0);
      existing.fees = (existing.fees ?? 0) + (r.fees ?? 0);
      existing.gross_notional = (existing.gross_notional ?? 0) + (r.gross_notional ?? 0);
      existing.win_count += r.win_count;
    }
  }

  // Compute per-strategy stats.
  const stats: Record<string, PerStratStats> = {};
  for (const name of Object.keys(STRATEGY_DETAIL)) {
    const filtered = signals.filter((s) => s.strategy.startsWith(name));
    const last = filtered[0];
    stats[name] = {
      signals: filtered.length,
      shareOfSignals: signals.length > 0 ? filtered.length / signals.length : 0,
      lastSeen: last?.ts ?? null,
      lastEdgeBps: last ? last.edge * 10_000 : null,
    };
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Strategies"
        subtitle={`${enabled.length} enabled · ${signals.length} signals in window`}
      />
      <div className="border-b border-terminal-border">
        <StrategiesPanel enabled={enabled} signals={signals} />
      </div>
      <div className="grid grid-cols-1 gap-2 overflow-auto p-2 xl:grid-cols-2 2xl:grid-cols-3">
        {Object.entries(STRATEGY_DETAIL).map(([name, meta]) => {
          const isOn = enabled.includes(name);
          const s = stats[name];
          return (
            <Panel key={name} title={name.replace("_", " ")} subtitle={meta.section}>
              <div className="flex flex-col gap-3 p-4">
                <div className="flex items-center gap-2">
                  <span
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider",
                      isOn
                        ? "border-terminal-green/40 bg-terminal-green/10 text-terminal-green"
                        : "border-terminal-border bg-terminal-bg text-terminal-dim",
                    )}
                  >
                    <span
                      className={cn(
                        "h-1.5 w-1.5 rounded-full",
                        isOn ? "bg-terminal-green" : "bg-terminal-dim",
                      )}
                    />
                    {isOn ? "Enabled" : "Disabled"}
                  </span>
                  <span className={cn("rounded border px-1.5 py-0.5 font-mono text-[10px]", meta.tone)}>
                    {meta.section}
                  </span>
                </div>
                <p className="font-mono text-[11px] leading-relaxed text-terminal-text">
                  {meta.description}
                </p>
                <div className="mt-2 grid grid-cols-3 gap-2 border-t border-terminal-border pt-3 font-mono text-[10px]">
                  <Stat label="Signals (window)" value={String(s.signals)} />
                  <Stat
                    label="Share"
                    value={`${(s.shareOfSignals * 100).toFixed(0)}%`}
                  />
                  <Stat
                    label="Last edge"
                    value={s.lastEdgeBps !== null ? `${s.lastEdgeBps.toFixed(0)} bps` : "—"}
                  />
                </div>
                {/* Per-strategy PNL attribution (30-day window).
                    Sourced from ledger_entries grouped by strategy base name. */}
                {(() => {
                  const pnl = pnlByStrategy[name];
                  if (!pnl || pnl.fill_count === 0) {
                    return (
                      <div className="font-mono text-[10px] text-terminal-dim">
                        no fills in 30d yet
                      </div>
                    );
                  }
                  const realized = pnl.realized_pnl ?? 0;
                  const winRate = pnl.fill_count > 0 ? pnl.win_count / pnl.fill_count : 0;
                  const realizedTone =
                    realized > 0
                      ? "text-terminal-green"
                      : realized < 0
                        ? "text-terminal-red"
                        : "text-terminal-text";
                  return (
                    <div className="grid grid-cols-4 gap-2 border-t border-terminal-border pt-3 font-mono text-[10px]">
                      <Stat label="Fills (30d)" value={String(pnl.fill_count)} />
                      <Stat
                        label="Realized"
                        value={formatUsd(realized, { showSign: true })}
                        tone={realizedTone}
                      />
                      <Stat
                        label="Win rate"
                        value={`${(winRate * 100).toFixed(0)}%`}
                      />
                      <Stat
                        label="Fees"
                        value={formatUsd(pnl.fees ?? 0)}
                      />
                    </div>
                  );
                })()}
                <div className="font-mono text-[10px] text-terminal-dim">
                  Last seen:{" "}
                  <span className="text-terminal-text">
                    {s.lastSeen ? relativeTime(s.lastSeen) : "never"}
                  </span>
                </div>
              </div>
            </Panel>
          );
        })}
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: string;
}) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-wider text-terminal-dim">{label}</div>
      <div className={cn("numeric mt-0.5 text-sm", tone ?? "text-terminal-textBright")}>
        {value}
      </div>
    </div>
  );
}
