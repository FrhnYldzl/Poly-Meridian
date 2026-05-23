"use client";

import { useSharedAgentState } from "@/hooks/use-shared-agent-state";
import { OrdersFeed } from "@/components/orders-feed";
import { PositionsTable } from "@/components/positions-table";
import { RiskPanel } from "@/components/risk-panel";
import { SignalsFeed } from "@/components/signals-feed";
import { SmartMoneyPanel } from "@/components/smart-money-panel";
import { StrategiesPanel } from "@/components/strategies-panel";

export default function OverviewPage() {
  const { snapshot } = useSharedAgentState();

  return (
    <div className="flex h-full flex-col">
      {/* Strategies row */}
      <div className="border-b border-terminal-border">
        <StrategiesPanel
          enabled={snapshot?.strategies_enabled ?? []}
          signals={snapshot?.last_signals ?? []}
        />
      </div>

      {/* 2×3 panel grid */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-2 p-2 lg:grid-cols-12 lg:grid-rows-2">
        <div className="min-h-0 lg:col-span-6">
          <PositionsTable positions={snapshot?.open_positions ?? []} />
        </div>
        <div className="min-h-0 lg:col-span-3">
          <SignalsFeed signals={snapshot?.last_signals ?? []} />
        </div>
        <div className="min-h-0 lg:col-span-3">
          <RiskPanel snapshot={snapshot} />
        </div>

        <div className="min-h-0 lg:col-span-6">
          <OrdersFeed orders={snapshot?.last_orders ?? []} />
        </div>
        <div className="min-h-0 lg:col-span-6">
          <SmartMoneyPanel clusters={snapshot?.smart_money_clusters ?? []} />
        </div>
      </div>
    </div>
  );
}
