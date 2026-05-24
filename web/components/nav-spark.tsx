"use client";

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

interface NavSparkProps {
  nav: number;
  height?: number;
  maxPoints?: number;
}

/** Tiny rolling NAV sparkline. Records `nav` on every prop change. */
export function NavSpark({ nav, height = 48, maxPoints = 120 }: NavSparkProps) {
  const [points, setPoints] = useState<number[]>([]);
  const lastRef = useRef<number>(0);

  useEffect(() => {
    if (nav === lastRef.current) return;
    lastRef.current = nav;
    setPoints((p) => [...p.slice(-(maxPoints - 1)), nav]);
  }, [nav, maxPoints]);

  if (points.length < 2) {
    return (
      <div
        className="grid-bg flex items-center justify-center rounded border border-terminal-border bg-terminal-surface text-[10px] uppercase tracking-wider text-terminal-dim"
        style={{ height }}
      >
        gathering data…
      </div>
    );
  }

  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  const last = points[points.length - 1];
  const first = points[0];
  const tone =
    last >= first ? "stroke-terminal-green fill-terminal-green/10" : "stroke-terminal-red fill-terminal-red/10";

  const w = 100;
  const h = 100;
  const xs = (i: number) => (i / (points.length - 1)) * w;
  const ys = (v: number) => h - ((v - min) / range) * h;
  const path = points.map((v, i) => `${i === 0 ? "M" : "L"} ${xs(i).toFixed(2)} ${ys(v).toFixed(2)}`).join(" ");
  const fillPath = `${path} L ${w} ${h} L 0 ${h} Z`;

  return (
    <div
      className="rounded border border-terminal-border bg-terminal-surface p-1"
      style={{ height }}
    >
      <svg
        viewBox={`0 0 ${w} ${h}`}
        preserveAspectRatio="none"
        className="h-full w-full"
      >
        <path d={fillPath} className={cn(tone, "fill-current stroke-none opacity-40")} />
        <path d={path} className={cn(tone, "fill-none stroke-current")} strokeWidth={1.5} />
      </svg>
    </div>
  );
}
