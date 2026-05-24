"use client";

import { Panel } from "./panel";
import { cn, formatUsd, formatCompact } from "@/lib/utils";
import type { Position } from "@/lib/types";

interface PositionsTableProps {
  positions: Position[];
}

export function PositionsTable({ positions }: PositionsTableProps) {
  return (
    <Panel
      title="Open positions"
      subtitle={`${positions.length} active`}
      hotkey="1"
      bodyClassName="font-mono text-[11px]"
    >
      {positions.length === 0 ? (
        <div className="flex h-full items-center justify-center text-terminal-dim">
          no open positions
        </div>
      ) : (
        <table className="w-full border-collapse text-left">
          <thead className="sticky top-0 z-10 bg-terminal-surfaceAlt text-[10px] uppercase tracking-wider text-terminal-dim">
            <tr>
              <th className="px-3 py-1.5 font-medium">Token</th>
              <th className="px-3 py-1.5 font-medium">Strategy</th>
              <th className="px-3 py-1.5 text-right font-medium">Qty</th>
              <th className="px-3 py-1.5 text-right font-medium">Avg cost</th>
              <th className="px-3 py-1.5 text-right font-medium">Mark</th>
              <th className="px-3 py-1.5 text-right font-medium">P&amp;L</th>
              <th className="px-3 py-1.5 text-right font-medium">Notional</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <Row key={p.token_id} p={p} />
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

function Row({ p }: { p: Position }) {
  const pnl = p.unrealized_pnl;
  const tone =
    pnl > 0 ? "text-terminal-green" : pnl < 0 ? "text-terminal-red" : "text-terminal-text";
  const notional = p.qty * p.last_mark;
  const strat = p.entry?.strategy ?? null;
  const entryPrice = p.entry?.entry_price ?? null;
  return (
    <tr className="border-t border-terminal-border/60 hover:bg-terminal-surfaceAlt/50">
      <td className="px-3 py-1.5 text-terminal-amber" title={p.token_id}>
        {p.token_id.slice(0, 12)}…
      </td>
      <td className="px-3 py-1.5">
        {strat ? (
          <span
            className="rounded border border-terminal-border px-1.5 py-0.5 text-[10px] text-terminal-text"
            title={
              entryPrice != null
                ? `entered @ ${entryPrice.toFixed(4)}${p.entry?.entry_ts ? ` · ${p.entry.entry_ts}` : ""}`
                : ""
            }
          >
            {strat}
          </span>
        ) : (
          <span className="text-[10px] text-terminal-dim">unknown</span>
        )}
      </td>
      <td className="numeric px-3 py-1.5 text-right">{formatCompact(p.qty)}</td>
      <td className="numeric px-3 py-1.5 text-right text-terminal-dim">
        {p.avg_cost.toFixed(4)}
      </td>
      <td className="numeric px-3 py-1.5 text-right">{p.last_mark.toFixed(4)}</td>
      <td className={cn("numeric px-3 py-1.5 text-right", tone)}>
        {formatUsd(pnl, { showSign: true })}
      </td>
      <td className="numeric px-3 py-1.5 text-right text-terminal-textBright">
        {formatUsd(notional)}
      </td>
    </tr>
  );
}
