"use client";

import { Panel } from "./panel";
import { StatusPill } from "./status-pill";
import { cn, formatPct } from "@/lib/utils";
import type { AgentSnapshot } from "@/lib/types";

interface RiskPanelProps {
  snapshot: AgentSnapshot | null;
}

export function RiskPanel({ snapshot }: RiskPanelProps) {
  const dailyPnl = snapshot?.daily_pnl_pct ?? 0;
  const totalExp = snapshot?.total_exposure_pct ?? 0;
  const ks = snapshot?.kill_switch_engaged ?? false;
  const reason = snapshot?.kill_switch_reason;

  const dailyTone = dailyPnl <= -0.04 ? "alert" : dailyPnl <= -0.02 ? "warn" : "ok";
  const expTone = totalExp >= 0.75 ? "alert" : totalExp >= 0.6 ? "warn" : "ok";

  return (
    <Panel title="Risk & limits" hotkey="6" bodyClassName="font-mono text-[11px]">
      <div className="flex flex-col gap-3 p-3">
        <div className="flex flex-wrap gap-2">
          <StatusPill
            tone={ks ? "alert" : "ok"}
            label={ks ? "KILL ENGAGED" : "KILL ARMED"}
            value={ks && reason ? reason : undefined}
            pulse={ks}
          />
          <StatusPill
            tone={dailyTone}
            label="DAILY P&L"
            value={formatPct(dailyPnl, { showSign: true })}
          />
          <StatusPill
            tone={expTone}
            label="TOTAL EXPOSURE"
            value={formatPct(totalExp)}
          />
        </div>

        <RiskMeter label="Total exposure" value={totalExp} cap={0.80} warn={0.60} />
        <RiskMeter
          label="Daily loss (vs cap)"
          value={Math.max(0, -dailyPnl)}
          cap={0.05}
          warn={0.03}
        />
        <RiskMeter
          label="Open positions"
          value={(snapshot?.open_position_count ?? 0) / 50}
          cap={1.0}
          warn={0.6}
          format={(v) => `${Math.round(v * 50)} / 50`}
        />
      </div>
    </Panel>
  );
}

function RiskMeter({
  label,
  value,
  cap,
  warn,
  format,
}: {
  label: string;
  value: number;
  cap: number;
  warn: number;
  format?: (v: number) => string;
}) {
  const pct = Math.min(1, value / cap);
  const tone = pct >= 1 ? "bg-terminal-red" : pct >= warn / cap ? "bg-terminal-yellow" : "bg-terminal-green";
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-[10px] uppercase tracking-wider">
        <span className="text-terminal-dim">{label}</span>
        <span className="numeric text-terminal-text">
          {format ? format(value) : formatPct(value)} /{" "}
          <span className="text-terminal-dim">{formatPct(cap)}</span>
        </span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-terminal-border/40">
        <div className={cn("h-full transition-all", tone)} style={{ width: `${pct * 100}%` }} />
      </div>
    </div>
  );
}
