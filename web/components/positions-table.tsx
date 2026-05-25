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
              <th className="px-3 py-1.5 font-medium">Market</th>
              <th className="px-3 py-1.5 font-medium">Outcome</th>
              <th className="px-3 py-1.5 font-medium">Strategy</th>
              <th
                className="px-3 py-1.5 text-right font-medium"
                title="Days until market resolves to $1 or $0. Capital is locked until then."
              >
                Resolves
              </th>
              <th className="px-3 py-1.5 text-right font-medium">Qty</th>
              <th className="px-3 py-1.5 text-right font-medium">Avg cost</th>
              <th className="px-3 py-1.5 text-right font-medium">Mark</th>
              <th
                className="px-3 py-1.5 text-right font-medium"
                title="Unrealized — liquidation value if you exit NOW. Will recover if thesis holds to resolution."
              >
                P&amp;L
              </th>
              <th className="px-3 py-1.5 text-right font-medium" title="Risk : reward — risking $1 to make ($1−p)/p">R:R</th>
              <th className="px-3 py-1.5 text-right font-medium" title="Max loss if token resolves to $0">Max loss</th>
              <th className="px-3 py-1.5 text-right font-medium" title="Max gain if token resolves to $1">Max gain</th>
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
  const tm = p.trade_metrics;
  // R:R coloring — symmetric (1.0) is neutral, > 1 = good asymmetry,
  // < 1 = paying too much for the favorite.
  const rrTone = tm
    ? tm.risk_reward_ratio >= 1.5
      ? "text-terminal-green"
      : tm.risk_reward_ratio < 0.5
        ? "text-terminal-red"
        : "text-terminal-amber"
    : "text-terminal-dim";
  const question = p.question ?? null;
  const polyUrl = p.polymarket_url ?? null;
  const marketTooltip = [
    question ? `market: ${question}` : null,
    p.outcome ? `outcome: ${p.outcome}` : null,
    p.condition_id ? `condition: ${p.condition_id}` : null,
    `token_id: ${p.token_id}`,
  ]
    .filter(Boolean)
    .join("\n");
  const outcomeTone =
    p.outcome === "Yes"
      ? "border-terminal-green/50 bg-terminal-green/10 text-terminal-green"
      : p.outcome === "No"
        ? "border-terminal-red/50 bg-terminal-red/10 text-terminal-red"
        : "border-terminal-border text-terminal-dim";
  return (
    <tr className="border-t border-terminal-border/60 hover:bg-terminal-surfaceAlt/50">
      <td
        className="max-w-[320px] px-3 py-1.5 text-terminal-text"
        title={marketTooltip}
      >
        <div className="flex items-center gap-1.5">
          <span className="truncate">
            {question ?? (
              <span className="text-terminal-amber">
                {p.token_id.slice(0, 12)}…
              </span>
            )}
          </span>
          {polyUrl && (
            <a
              href={polyUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="shrink-0 text-[10px] text-terminal-dim hover:text-terminal-amber"
              title="Open on Polymarket"
              onClick={(e) => e.stopPropagation()}
            >
              ↗
            </a>
          )}
        </div>
      </td>
      <td className="px-3 py-1.5">
        {p.outcome ? (
          <span
            className={cn(
              "rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider",
              outcomeTone,
            )}
          >
            {p.outcome}
          </span>
        ) : (
          <span className="text-[10px] text-terminal-dim">—</span>
        )}
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
      <td
        className={cn(
          "numeric px-3 py-1.5 text-right",
          p.days_to_resolution == null
            ? "text-terminal-dim"
            : p.days_to_resolution < 2
              ? "text-terminal-amber"
              : p.days_to_resolution > 60
                ? "text-terminal-red"
                : "text-terminal-text",
        )}
        title={p.end_date_iso ?? ""}
      >
        {p.days_to_resolution == null
          ? "—"
          : p.days_to_resolution < 1
            ? `${(p.days_to_resolution * 24).toFixed(1)}h`
            : `${p.days_to_resolution.toFixed(1)}d`}
      </td>
      <td className="numeric px-3 py-1.5 text-right">{formatCompact(p.qty)}</td>
      <td className="numeric px-3 py-1.5 text-right text-terminal-dim">
        {p.avg_cost.toFixed(4)}
      </td>
      <td className="numeric px-3 py-1.5 text-right">{p.last_mark.toFixed(4)}</td>
      <td className={cn("numeric px-3 py-1.5 text-right", tone)}>
        {formatUsd(pnl, { showSign: true })}
      </td>
      <td className={cn("numeric px-3 py-1.5 text-right", rrTone)}>
        {tm ? tm.risk_reward_ratio.toFixed(2) : "—"}
      </td>
      <td className="numeric px-3 py-1.5 text-right text-terminal-red/80">
        {tm ? formatUsd(tm.max_loss_usd) : "—"}
      </td>
      <td className="numeric px-3 py-1.5 text-right text-terminal-green/80">
        {tm ? formatUsd(tm.max_gain_usd) : "—"}
      </td>
      <td className="numeric px-3 py-1.5 text-right text-terminal-textBright">
        {formatUsd(notional)}
      </td>
    </tr>
  );
}
