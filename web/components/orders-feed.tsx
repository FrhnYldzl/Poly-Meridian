"use client";

import { Panel } from "./panel";
import { cn, relativeTime } from "@/lib/utils";
import type { OrderRow } from "@/lib/types";

interface OrdersFeedProps {
  orders: OrderRow[];
}

export function OrdersFeed({ orders }: OrdersFeedProps) {
  return (
    <Panel
      title="Recent orders"
      subtitle={`${orders.length} most recent`}
      hotkey="3"
      bodyClassName="font-mono text-[11px]"
    >
      {orders.length === 0 ? (
        <div className="flex h-full items-center justify-center text-terminal-dim">
          no orders yet
        </div>
      ) : (
        <table className="w-full border-collapse text-left">
          <thead className="sticky top-0 z-10 bg-terminal-surfaceAlt text-[10px] uppercase tracking-wider text-terminal-dim">
            <tr>
              <th className="px-3 py-1.5 font-medium">Time</th>
              <th className="px-3 py-1.5 font-medium">Strategy</th>
              <th className="px-3 py-1.5 font-medium">Side</th>
              <th className="px-3 py-1.5 text-right font-medium">Price</th>
              <th className="px-3 py-1.5 text-right font-medium">Size</th>
              <th className="px-3 py-1.5 font-medium">Status</th>
              <th className="px-3 py-1.5 font-medium">Mode</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((o, i) => (
              <Row key={`${o.order_id}-${i}`} o={o} />
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

function Row({ o }: { o: OrderRow }) {
  const sideTone =
    o.side === "BUY" || o.side?.toString().endsWith(".BUY")
      ? "text-terminal-green"
      : "text-terminal-red";
  const statusRaw = (o.status ?? "").toString().replace(/^OrderStatus\./, "");
  const statusTone =
    {
      FILLED: "text-terminal-green",
      PARTIAL: "text-terminal-yellow",
      LIVE: "text-terminal-cyan",
      PENDING: "text-terminal-dim",
      CANCELLED: "text-terminal-dim",
      REJECTED: "text-terminal-red",
    }[statusRaw as keyof object] ?? "text-terminal-text";
  // Tooltip captures full attribution: market + edge + conviction + contributors
  const tipBody = [
    o.market_question ? `market: ${o.market_question}` : null,
    o.condition_id ? `condition: ${o.condition_id}` : null,
    o.contributors?.length
      ? `contributors: ${o.contributors.join(", ")}`
      : null,
    o.edge != null ? `edge: ${o.edge.toFixed(4)}` : null,
    o.conviction != null ? `conviction: ${o.conviction.toFixed(2)}` : null,
    o.size_pct != null ? `size_pct: ${(o.size_pct * 100).toFixed(2)}%` : null,
    `order_id: ${o.order_id}`,
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <tr
      className="border-t border-terminal-border/60 hover:bg-terminal-surfaceAlt/50"
      title={tipBody}
    >
      <td className="px-3 py-1.5 text-terminal-dim">{relativeTime(o.ts)}</td>
      <td className="px-3 py-1.5 text-terminal-amber">
        {o.strategy?.toString().split(".")[0] ?? "?"}
      </td>
      <td className={cn("px-3 py-1.5 font-semibold", sideTone)}>
        {o.side?.toString().split(".").pop()}
      </td>
      <td className="numeric px-3 py-1.5 text-right">{o.price?.toFixed(4) ?? "—"}</td>
      <td className="numeric px-3 py-1.5 text-right">{o.size?.toFixed(2) ?? "—"}</td>
      <td className={cn("px-3 py-1.5 uppercase tracking-wider", statusTone)}>
        {statusRaw}
      </td>
      <td className="px-3 py-1.5 text-terminal-dim">
        {o.mode?.toString().split(".").pop()}
      </td>
    </tr>
  );
}
