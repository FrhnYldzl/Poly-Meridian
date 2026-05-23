"use client";

import { PageHeader } from "@/components/page-header";
import { Panel } from "@/components/panel";
import { SmartMoneyPanel } from "@/components/smart-money-panel";
import { useSharedAgentState } from "@/hooks/use-shared-agent-state";

export default function SmartMoneyPage() {
  const { snapshot } = useSharedAgentState();
  const clusters = snapshot?.smart_money_clusters ?? [];
  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Smart Money"
        subtitle={`${clusters.length} active clusters — 3-tier follow-on`}
        badge="Phase 5a"
      />
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-2 p-2 xl:grid-cols-12 xl:grid-rows-2">
        <div className="min-h-0 xl:col-span-7 xl:row-span-2">
          <SmartMoneyPanel clusters={clusters} />
        </div>
        <Panel
          title="Tracked wallets"
          subtitle="leaderboard scrape · per-tier counts"
          className="min-h-0 xl:col-span-5"
        >
          <div className="grid grid-cols-3 gap-2 p-4">
            <TierCard label="Tier 1" tone="green" count={0} desc="proven · auto-trade · full Kelly" />
            <TierCard label="Tier 2" tone="yellow" count={0} desc="hot · auto-trade · half Kelly" />
            <TierCard label="Tier 3" tone="cyan" count={0} desc="observation only" />
          </div>
        </Panel>
        <Panel
          title="Filter rejections"
          subtitle="why we didn't copy"
          className="min-h-0 xl:col-span-5"
        >
          <div className="flex h-full items-center justify-center px-6 py-10 text-center">
            <p className="max-w-md font-mono text-[11px] leading-relaxed text-terminal-dim">
              Latency decay · hedge flag · drawdown filter · min net USD ·
              per-trader concentration cap. Counters wire in Phase 7b.
            </p>
          </div>
        </Panel>
      </div>
    </div>
  );
}

function TierCard({
  label,
  tone,
  count,
  desc,
}: {
  label: string;
  tone: "green" | "yellow" | "cyan";
  count: number;
  desc: string;
}) {
  const tones = {
    green: "border-terminal-green/40 bg-terminal-green/10 text-terminal-green",
    yellow: "border-terminal-yellow/40 bg-terminal-yellow/10 text-terminal-yellow",
    cyan: "border-terminal-cyan/40 bg-terminal-cyan/10 text-terminal-cyan",
  } as const;
  return (
    <div className={"flex flex-col rounded border bg-terminal-bg p-3 " + tones[tone]}>
      <span className="text-[10px] uppercase tracking-wider">{label}</span>
      <span className="numeric mt-1 text-2xl font-semibold">{count}</span>
      <span className="text-[9px] leading-tight text-terminal-dim">{desc}</span>
    </div>
  );
}
