"use client";

import { useMemo } from "react";
import { CATEGORY_COLOR } from "./markets-coverage-panel";
import { cn, formatUsd } from "@/lib/utils";

interface AllocationSlice {
  label: string;
  value: number;          // dollars
  tone: string;           // tailwind text-color class
}

interface AllocationDonutProps {
  slices: AllocationSlice[];
  centerValue: string;
  centerLabel: string;
  size?: number;
}

const TAILWIND_TO_SVG: Record<string, string> = {
  "text-terminal-green": "rgb(34, 197, 94)",
  "text-terminal-red": "rgb(239, 68, 68)",
  "text-terminal-amber": "rgb(245, 158, 11)",
  "text-terminal-cyan": "rgb(6, 182, 212)",
  "text-terminal-purple": "rgb(168, 85, 247)",
  "text-terminal-yellow": "rgb(234, 179, 8)",
  "text-terminal-dim": "rgb(82, 89, 99)",
  "text-terminal-text": "rgb(170, 180, 195)",
};

/** Lightweight SVG donut — no chart library. Slices ordered by value DESC. */
export function AllocationDonut({
  slices,
  centerValue,
  centerLabel,
  size = 220,
}: AllocationDonutProps) {
  const total = slices.reduce((s, x) => s + x.value, 0);
  const sorted = useMemo(
    () => [...slices].filter((s) => s.value > 0).sort((a, b) => b.value - a.value),
    [slices],
  );

  if (total === 0 || sorted.length === 0) {
    return (
      <div
        className="grid-bg flex items-center justify-center rounded border border-terminal-border bg-terminal-surface text-[10px] uppercase tracking-wider text-terminal-dim"
        style={{ height: size }}
      >
        no positions yet
      </div>
    );
  }

  // SVG donut math — angles in radians, polar to cartesian.
  const cx = 50;
  const cy = 50;
  const rOuter = 42;
  const rInner = 28;
  let cumStart = -Math.PI / 2;  // start at 12 o'clock

  const arcs = sorted.map((s) => {
    const frac = s.value / total;
    const angle = frac * 2 * Math.PI;
    const start = cumStart;
    const end = cumStart + angle;
    cumStart = end;

    const x1 = cx + rOuter * Math.cos(start);
    const y1 = cy + rOuter * Math.sin(start);
    const x2 = cx + rOuter * Math.cos(end);
    const y2 = cy + rOuter * Math.sin(end);
    const x3 = cx + rInner * Math.cos(end);
    const y3 = cy + rInner * Math.sin(end);
    const x4 = cx + rInner * Math.cos(start);
    const y4 = cy + rInner * Math.sin(start);
    const large = angle > Math.PI ? 1 : 0;

    return {
      d: [
        `M ${x1.toFixed(3)} ${y1.toFixed(3)}`,
        `A ${rOuter} ${rOuter} 0 ${large} 1 ${x2.toFixed(3)} ${y2.toFixed(3)}`,
        `L ${x3.toFixed(3)} ${y3.toFixed(3)}`,
        `A ${rInner} ${rInner} 0 ${large} 0 ${x4.toFixed(3)} ${y4.toFixed(3)}`,
        "Z",
      ].join(" "),
      color: TAILWIND_TO_SVG[s.tone] ?? "rgb(170, 180, 195)",
      label: s.label,
      value: s.value,
      pct: frac * 100,
    };
  });

  return (
    <div className="flex items-center gap-4">
      <svg
        viewBox="0 0 100 100"
        width={size}
        height={size}
        className="shrink-0"
      >
        {arcs.map((a, i) => (
          <path
            key={i}
            d={a.d}
            fill={a.color}
            opacity={0.85}
          >
            <title>{`${a.label}: ${formatUsd(a.value)} (${a.pct.toFixed(1)}%)`}</title>
          </path>
        ))}
        {/* center label */}
        <text
          x="50"
          y="46"
          textAnchor="middle"
          className="fill-current text-terminal-textBright"
          style={{ font: "600 6px ui-monospace, monospace" }}
        >
          {centerValue}
        </text>
        <text
          x="50"
          y="55"
          textAnchor="middle"
          className="fill-current text-terminal-dim"
          style={{ font: "500 3px ui-monospace, monospace", letterSpacing: "0.1em" }}
        >
          {centerLabel.toUpperCase()}
        </text>
      </svg>
      <ul className="flex flex-1 flex-col gap-1 font-mono text-[11px]">
        {arcs.map((a, i) => (
          <li
            key={i}
            className="grid grid-cols-[12px_1fr_auto_50px] items-center gap-2"
          >
            <span
              className="h-2.5 w-2.5 rounded-sm"
              style={{ backgroundColor: a.color }}
            />
            <span className={cn("truncate", a.label in CATEGORY_COLOR ? CATEGORY_COLOR[a.label] : "text-terminal-text")}>
              {a.label}
            </span>
            <span className="text-terminal-dim">{a.pct.toFixed(1)}%</span>
            <span className="numeric text-right text-terminal-textBright">
              {formatUsd(a.value)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
