"use client";

import { useEffect, useRef, useState } from "react";
import { HeaderBar } from "@/components/header-bar";
import { OrdersFeed } from "@/components/orders-feed";
import { PositionsTable } from "@/components/positions-table";
import { RiskPanel } from "@/components/risk-panel";
import { SignalsFeed } from "@/components/signals-feed";
import { SmartMoneyPanel } from "@/components/smart-money-panel";
import { StrategiesPanel } from "@/components/strategies-panel";
import { disengageKillSwitch, engageKillSwitch } from "@/lib/api";
import { useAgentState } from "@/hooks/use-agent-state";

export default function Page() {
  const { snapshot, connected, lastUpdateMs } = useAgentState();
  const [focusIdx, setFocusIdx] = useState(0);
  const panelRefs = [
    useRef<HTMLDivElement>(null),
    useRef<HTMLDivElement>(null),
    useRef<HTMLDivElement>(null),
    useRef<HTMLDivElement>(null),
    useRef<HTMLDivElement>(null),
    useRef<HTMLDivElement>(null),
  ];

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // Don't capture when typing in inputs.
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA")) return;

      // Number keys 1..6 focus panels.
      const n = Number(e.key);
      if (n >= 1 && n <= 6) {
        e.preventDefault();
        setFocusIdx(n - 1);
        panelRefs[n - 1]?.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
        return;
      }
      // K — toggle kill switch.
      if (e.key === "k" || e.key === "K") {
        e.preventDefault();
        toggleKillSwitch();
        return;
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
    // panelRefs is stable; suppress exhaustive-deps complaint.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshot]);

  const toggleKillSwitch = async () => {
    if (!snapshot) return;
    const ok = snapshot.kill_switch_engaged
      ? confirm("Disengage kill-switch? New orders will resume.")
      : confirm("Engage kill-switch? New orders will be rejected.");
    if (!ok) return;
    try {
      if (snapshot.kill_switch_engaged) {
        await disengageKillSwitch();
      } else {
        await engageKillSwitch("manual via UI");
      }
    } catch (e) {
      alert(`Kill-switch toggle failed: ${e}`);
    }
  };

  return (
    <div className="flex h-screen flex-col bg-terminal-bg text-terminal-text">
      <HeaderBar
        snapshot={snapshot}
        connected={connected}
        lastUpdateMs={lastUpdateMs}
        onKillSwitchToggle={toggleKillSwitch}
      />

      {/* Strategies bar — always visible, dense. */}
      <div className="border-b border-terminal-border">
        <StrategiesPanel
          enabled={snapshot?.strategies_enabled ?? []}
          signals={snapshot?.last_signals ?? []}
        />
      </div>

      {/* Main grid: 2 rows × 3 cols on xl, stacks on smaller. */}
      <main className="grid min-h-0 flex-1 grid-cols-1 gap-2 p-2 lg:grid-cols-12 lg:grid-rows-2">
        <div ref={panelRefs[0]} className="min-h-0 lg:col-span-6">
          <PositionsTable positions={snapshot?.open_positions ?? []} />
        </div>
        <div ref={panelRefs[1]} className="min-h-0 lg:col-span-3">
          <SignalsFeed signals={snapshot?.last_signals ?? []} />
        </div>
        <div ref={panelRefs[5]} className="min-h-0 lg:col-span-3">
          <RiskPanel snapshot={snapshot} />
        </div>

        <div ref={panelRefs[2]} className="min-h-0 lg:col-span-6">
          <OrdersFeed orders={snapshot?.last_orders ?? []} />
        </div>
        <div ref={panelRefs[3]} className="min-h-0 lg:col-span-6">
          <SmartMoneyPanel clusters={snapshot?.smart_money_clusters ?? []} />
        </div>
      </main>

      <footer className="flex items-center justify-between border-t border-terminal-border bg-terminal-surfaceAlt px-3 py-1 font-mono text-[10px] uppercase tracking-wider text-terminal-dim">
        <span>
          [1]POSITIONS [2]SIGNALS [3]ORDERS [4]SMART-MONEY [5]STRATS [6]RISK [K]KILL
        </span>
        <span>
          UPTIME{" "}
          <span className="numeric text-terminal-text">
            {formatUptime(snapshot?.uptime_sec ?? 0)}
          </span>
        </span>
      </footer>
    </div>
  );
}

function formatUptime(s: number): string {
  if (!s) return "—";
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}
