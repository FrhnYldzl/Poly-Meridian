"use client";

import { useMemo } from "react";
import { cn, formatUsd } from "@/lib/utils";
import type { NavPoint } from "@/hooks/use-nav-history";

interface EquityChartProps {
  points: NavPoint[];
  startingNav?: number;
  height?: number;
}

/** Alpaca-style equity curve.
 *  - Line + filled-area, tone driven by sign of total return.
 *  - Y-axis: min / max gridlines (no clutter).
 *  - X-axis: first + last timestamp.
 *  - Horizontal dashed line at starting NAV so the operator sees crossings.
 */
export function EquityChart({
  points,
  startingNav,
  height = 320,
}: EquityChartProps) {
  const stats = useMemo(() => {
    if (points.length < 2) return null;
    const navs = points.map((p) => p.nav);
    const min = Math.min(...navs);
    const max = Math.max(...navs);
    const first = points[0].nav;
    const last = points[points.length - 1].nav;
    return { min, max, first, last };
  }, [points]);

  if (!stats || points.length < 2) {
    return (
      <div
        className="grid-bg flex items-center justify-center rounded border border-terminal-border bg-terminal-surface text-[11px] uppercase tracking-wider text-terminal-dim"
        style={{ height }}
      >
        gathering NAV — leave the tab open
      </div>
    );
  }

  const { min, max, first, last } = stats;
  const base = startingNav ?? first;
  // Pad y-range so dashed baseline isn't on the top/bottom edge.
  const padded = {
    lo: Math.min(min, base) - (max - min) * 0.05,
    hi: Math.max(max, base) + (max - min) * 0.05,
  };
  const range = padded.hi - padded.lo || 1;
  const totalReturn = (last - base) / base;
  const tone =
    last >= base
      ? "stroke-terminal-green fill-terminal-green/15"
      : "stroke-terminal-red fill-terminal-red/15";

  const w = 1000;
  const h = 300;
  const xs = (i: number) => (i / (points.length - 1)) * w;
  const ys = (v: number) => h - ((v - padded.lo) / range) * h;

  const linePath = points
    .map((p, i) => `${i === 0 ? "M" : "L"} ${xs(i).toFixed(2)} ${ys(p.nav).toFixed(2)}`)
    .join(" ");
  const areaPath = `${linePath} L ${w} ${h} L 0 ${h} Z`;
  const baseY = ys(base);

  const firstTs = new Date(points[0].ts).toLocaleString();
  const lastTs = new Date(points[points.length - 1].ts).toLocaleString();

  return (
    <div
      className="relative overflow-hidden rounded border border-terminal-border bg-terminal-surface"
      style={{ height }}
    >
      {/* Title row */}
      <div className="absolute left-3 top-2 z-10 font-mono text-[11px] uppercase tracking-wider text-terminal-dim">
        Equity curve
      </div>
      <div className="absolute right-3 top-2 z-10 flex items-baseline gap-2 font-mono">
        <span className="text-[11px] uppercase tracking-wider text-terminal-dim">total</span>
        <span
          className={cn(
            "numeric text-[13px] font-semibold",
            totalReturn >= 0 ? "text-terminal-green" : "text-terminal-red",
          )}
        >
          {totalReturn >= 0 ? "+" : ""}
          {(totalReturn * 100).toFixed(2)}%
        </span>
      </div>

      {/* SVG body fills the panel */}
      <svg
        viewBox={`0 0 ${w} ${h}`}
        preserveAspectRatio="none"
        className="absolute inset-0 h-full w-full"
      >
        {/* Filled area */}
        <path d={areaPath} className={cn(tone, "fill-current stroke-none")} />
        {/* Line */}
        <path
          d={linePath}
          className={cn(tone, "fill-none stroke-current")}
          strokeWidth={1.5}
          vectorEffect="non-scaling-stroke"
        />
        {/* Baseline (starting NAV) */}
        <line
          x1={0}
          x2={w}
          y1={baseY}
          y2={baseY}
          className="stroke-terminal-dim/60"
          strokeWidth={1}
          strokeDasharray="4 4"
          vectorEffect="non-scaling-stroke"
        />
      </svg>

      {/* Bottom axis labels */}
      <div className="absolute bottom-1 left-3 right-3 z-10 flex items-end justify-between font-mono text-[9px] text-terminal-dim">
        <span>{firstTs}</span>
        <span className="text-terminal-textBright">{formatUsd(last)}</span>
        <span>{lastTs}</span>
      </div>
      {/* Side y-axis hints */}
      <div className="absolute right-2 z-10 flex flex-col gap-1 pt-7 text-right font-mono text-[9px] text-terminal-dim">
        <span>max {formatUsd(max)}</span>
        <span>base {formatUsd(base)}</span>
        <span>min {formatUsd(min)}</span>
      </div>
    </div>
  );
}
