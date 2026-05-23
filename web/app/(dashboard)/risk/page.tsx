"use client";

import { PageHeader } from "@/components/page-header";
import { RiskPanel } from "@/components/risk-panel";
import { useSharedAgentState } from "@/hooks/use-shared-agent-state";

export default function RiskPage() {
  const { snapshot } = useSharedAgentState();
  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Risk & limits"
        subtitle="Kelly · exposure caps · kill-switch · drawdown"
      />
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-2 p-2 lg:grid-cols-12">
        <div className="min-h-0 lg:col-span-6">
          <RiskPanel snapshot={snapshot} />
        </div>
        <div className="min-h-0 lg:col-span-6 grid-bg rounded-md border border-terminal-border bg-terminal-surface p-6">
          <h3 className="font-mono text-[11px] uppercase tracking-wider text-terminal-textBright">
            Kill-switch triggers
          </h3>
          <ul className="mt-3 space-y-1.5 font-mono text-[11px] text-terminal-text">
            <li>• Daily loss &gt; 5%</li>
            <li>• Slippage anomaly &gt; 200 bps</li>
            <li>• API error rate &gt; 5% (over 20-call window)</li>
            <li>• WebSocket disconnect &gt; 60s</li>
            <li>• Wallet balance mismatch</li>
            <li>• Manual operator engage (UI button or CLI)</li>
          </ul>
          <p className="mt-4 font-mono text-[10px] leading-relaxed text-terminal-dim">
            Manual disengage only. Operator must investigate root cause before
            re-arming. See <span className="text-terminal-amber">scripts/dr_drill.py</span> for the drill checklist.
          </p>
        </div>
      </div>
    </div>
  );
}
