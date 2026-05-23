"use client";

import { PageHeader } from "@/components/page-header";
import { Panel } from "@/components/panel";
import { StrategiesPanel } from "@/components/strategies-panel";
import { useSharedAgentState } from "@/hooks/use-shared-agent-state";

const STRATEGY_DETAIL: Record<string, { description: string; section: string }> = {
  arbitrage: {
    description:
      "Single-market complete-set arb. Detects YES_ask + NO_ask < 1 (after worst-case fees). Tight cluster confirmation, maker-first.",
    section: "§14.1",
  },
  sentiment: {
    description:
      "News-driven directional bets. OpenAI embeddings + Claude scorer; impact-weighted sentiment shift on book mid.",
    section: "§14.2",
  },
  smart_money: {
    description:
      "3-tier follow-on of top Polymarket wallets. Latency decay 30min, per-trader cap 5%, drawdown filter, attribution log.",
    section: "§14.3",
  },
  stat_quant: {
    description:
      "Four sub-signals: mean reversion (z-score), momentum (lookback + volume gate), vol breakout, time decay <24h.",
    section: "§14.4",
  },
  fundamentals: {
    description:
      "Category-specific probability models: Politics (poll aggregator), Sports (Elo), Crypto (TA + funding + netflow), Macro (calendar).",
    section: "§14.5",
  },
};

export default function StrategiesPage() {
  const { snapshot } = useSharedAgentState();
  const enabled = snapshot?.strategies_enabled ?? [];
  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Strategies"
        subtitle={`${enabled.length} enabled — capacity & attribution per strategy`}
        badge="Phase 7b"
      />
      <div className="border-b border-terminal-border">
        <StrategiesPanel enabled={enabled} signals={snapshot?.last_signals ?? []} />
      </div>
      <div className="grid grid-cols-1 gap-2 overflow-auto p-2 xl:grid-cols-2">
        {Object.entries(STRATEGY_DETAIL).map(([name, meta]) => {
          const isOn = enabled.includes(name);
          const sigCount =
            (snapshot?.last_signals ?? []).filter((s) => s.strategy === name).length;
          return (
            <Panel key={name} title={name.replace("_", " ")} subtitle={meta.section}>
              <div className="flex flex-col gap-3 p-4">
                <div className="flex items-center gap-2">
                  <span
                    className={
                      "inline-flex items-center gap-1.5 rounded border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider " +
                      (isOn
                        ? "border-terminal-green/40 bg-terminal-green/10 text-terminal-green"
                        : "border-terminal-border bg-terminal-bg text-terminal-dim")
                    }
                  >
                    <span
                      className={
                        "h-1.5 w-1.5 rounded-full " +
                        (isOn ? "bg-terminal-green" : "bg-terminal-dim")
                      }
                    />
                    {isOn ? "Enabled" : "Disabled"}
                  </span>
                  <span className="font-mono text-[11px] text-terminal-dim">
                    {sigCount} recent signals
                  </span>
                </div>
                <p className="font-mono text-[11px] leading-relaxed text-terminal-text">
                  {meta.description}
                </p>
                <div className="mt-2 grid grid-cols-3 gap-2 border-t border-terminal-border pt-3 font-mono text-[10px]">
                  <Stat label="Daily PnL" value="—" />
                  <Stat label="Win rate" value="—" />
                  <Stat label="Trades 24h" value="—" />
                </div>
              </div>
            </Panel>
          );
        })}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-wider text-terminal-dim">{label}</div>
      <div className="numeric mt-0.5 text-sm text-terminal-textBright">{value}</div>
    </div>
  );
}
