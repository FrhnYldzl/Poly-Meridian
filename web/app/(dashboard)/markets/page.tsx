"use client";

import { PageHeader } from "@/components/page-header";
import { Panel } from "@/components/panel";
import { useSharedAgentState } from "@/hooks/use-shared-agent-state";

export default function MarketsPage() {
  const { snapshot } = useSharedAgentState();
  const count = snapshot?.markets_watched ?? 0;
  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Markets"
        subtitle={`${count} active markets watched`}
        badge="Phase 7c"
      />
      <div className="grid-bg flex-1 p-2">
        <Panel title="Watchlist" subtitle="real-time book depth across tracked markets">
          <div className="flex h-full flex-col items-center justify-center gap-1 px-6 py-10 text-center">
            <span className="font-mono text-[10px] uppercase tracking-[0.3em] text-terminal-amber">
              In progress
            </span>
            <p className="max-w-md font-mono text-[11px] leading-relaxed text-terminal-dim">
              Top-{count > 0 ? count : 40} markets the agent is subscribed to via
              WebSocket, with mid / spread / depth / volume / time-to-resolution.
              Filters: category, liquidity, end-date, custom watchlist.
            </p>
          </div>
        </Panel>
      </div>
    </div>
  );
}
