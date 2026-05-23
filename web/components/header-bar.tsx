"use client";

import { StatusPill } from "./status-pill";
import { formatPct, formatUsd, relativeTime } from "@/lib/utils";
import type { AgentSnapshot } from "@/lib/types";

interface HeaderBarProps {
  snapshot: AgentSnapshot | null;
  connected: boolean;
  lastUpdateMs: number;
  onKillSwitchToggle?: () => void;
}

export function HeaderBar({ snapshot, connected, lastUpdateMs, onKillSwitchToggle }: HeaderBarProps) {
  const modeTone = snapshot?.mode?.startsWith("live") ? "alert" : "info";
  const ksTone = snapshot?.kill_switch_engaged ? "alert" : "ok";
  const pnlPositive = (snapshot?.daily_pnl_pct ?? 0) >= 0;

  return (
    <header className="border-b border-terminal-border bg-terminal-surfaceAlt">
      <div className="flex flex-wrap items-stretch gap-4 px-4 py-2">
        <div className="flex items-center gap-3">
          <div className="flex items-baseline gap-2">
            <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-terminal-dim">
              POLY • MERIDIAN
            </span>
            <span className="font-mono text-[10px] text-terminal-amber/80">v0.1.0</span>
          </div>
          <StatusPill
            tone={modeTone}
            label="MODE"
            value={snapshot?.mode ?? "—"}
            pulse={snapshot?.mode?.startsWith("live")}
          />
        </div>

        <div className="flex flex-1 items-center gap-6">
          <StatBlock label="NAV (USD)" value={formatUsd(snapshot?.nav_usd ?? 0)} highlight />
          <StatBlock label="CASH" value={formatUsd(snapshot?.cash_usd ?? 0)} />
          <StatBlock
            label="DAILY P&L"
            value={formatPct(snapshot?.daily_pnl_pct ?? 0, { showSign: true })}
            tone={pnlPositive ? "positive" : "negative"}
          />
          <StatBlock
            label="EXPOSURE"
            value={formatPct(snapshot?.total_exposure_pct ?? 0)}
          />
          <StatBlock label="OPEN POS" value={String(snapshot?.open_position_count ?? 0)} />
          <StatBlock label="MARKETS" value={String(snapshot?.markets_watched ?? 0)} />
        </div>

        <div className="flex items-center gap-2">
          <StatusPill
            tone={connected ? "ok" : "warn"}
            label={connected ? "LIVE" : "RECONNECTING"}
            value={
              lastUpdateMs
                ? relativeTime(new Date(lastUpdateMs).toISOString())
                : "—"
            }
          />
          <button
            type="button"
            onClick={onKillSwitchToggle}
            className={
              "flex items-center gap-1.5 rounded border px-2 py-1 font-mono text-[11px] uppercase tracking-wider transition " +
              (snapshot?.kill_switch_engaged
                ? "border-terminal-red bg-terminal-red/20 text-terminal-red hover:bg-terminal-red/30"
                : "border-terminal-border bg-terminal-bg text-terminal-text hover:border-terminal-red hover:text-terminal-red")
            }
            title="Toggle kill-switch (K)"
          >
            <span
              className={
                "h-1.5 w-1.5 rounded-full " +
                (snapshot?.kill_switch_engaged ? "bg-terminal-red animate-blink" : "bg-terminal-green")
              }
            />
            KILL {snapshot?.kill_switch_engaged ? "ENGAGED" : "ARMED"}
          </button>
        </div>
      </div>
    </header>
  );
}

function StatBlock({
  label,
  value,
  tone,
  highlight,
}: {
  label: string;
  value: string;
  tone?: "positive" | "negative";
  highlight?: boolean;
}) {
  const valColor =
    tone === "positive"
      ? "text-terminal-green"
      : tone === "negative"
        ? "text-terminal-red"
        : highlight
          ? "text-terminal-amber"
          : "text-terminal-textBright";
  return (
    <div className="flex flex-col leading-tight">
      <span className="font-mono text-[10px] uppercase tracking-wider text-terminal-dim">
        {label}
      </span>
      <span className={"numeric font-mono text-base font-semibold " + valColor}>
        {value}
      </span>
    </div>
  );
}
