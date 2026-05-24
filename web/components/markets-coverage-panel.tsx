"use client";

import { Panel } from "./panel";
import type { AgentSnapshot } from "@/lib/types";

interface MarketsCoveragePanelProps {
  snapshot: AgentSnapshot | null;
}

// Canonical Polymarket categories — we render in this order so the operator
// gets a stable view. Anything from Gamma we don't recognize falls into
// "Other" via the Python aggregator, so all counts always sum to the total.
const CATEGORY_ORDER = [
  "Politics",
  "Sports",
  "Crypto",
  "Pop Culture",
  "Business",
  "Science",
  "Tech",
  "Climate",
  "Other",
] as const;

// Color hint per category. Re-use across the dashboard so a "Sports" badge
// always looks the same wherever it appears.
export const CATEGORY_COLOR: Record<string, string> = {
  Politics: "text-terminal-cyan",
  Sports: "text-terminal-green",
  Crypto: "text-terminal-amber",
  "Pop Culture": "text-terminal-purple",
  Business: "text-terminal-yellow",
  Science: "text-terminal-text",
  Tech: "text-terminal-cyan",
  Climate: "text-terminal-green",
  Other: "text-terminal-dim",
};

export function MarketsCoveragePanel({ snapshot }: MarketsCoveragePanelProps) {
  const byCat = snapshot?.markets_by_category ?? {};
  const totalActive = snapshot?.markets_active_total ?? 0;
  const wsCount = snapshot?.ws_subscribed_total ?? snapshot?.markets_watched ?? 0;

  // Merge known-order categories + any extras Gamma surfaces, suppressing
  // zero-count entries so we don't show ghost rows in the panel.
  const seen = new Set<string>();
  const ordered: [string, number][] = [];
  for (const cat of CATEGORY_ORDER) {
    if (byCat[cat] != null && byCat[cat] > 0) {
      ordered.push([cat, byCat[cat]]);
      seen.add(cat);
    }
  }
  for (const [cat, n] of Object.entries(byCat)) {
    if (!seen.has(cat) && n > 0) ordered.push([cat, n]);
  }

  // Largest count drives the bar widths.
  const maxCount = ordered.reduce((m, [, n]) => (n > m ? n : m), 1);

  return (
    <Panel
      title="Markets coverage"
      subtitle={`${wsCount}/${totalActive} on WS · ${ordered.length} categories`}
      bodyClassName="font-mono text-[11px]"
    >
      {ordered.length === 0 ? (
        <div className="flex h-full items-center justify-center text-terminal-dim">
          gamma_sync hasn&apos;t run yet…
        </div>
      ) : (
        <div className="flex flex-col gap-1.5 p-3">
          {ordered.map(([cat, n]) => {
            const pct = (n / maxCount) * 100;
            const tone = CATEGORY_COLOR[cat] ?? "text-terminal-text";
            return (
              <div key={cat} className="grid grid-cols-[100px_1fr_40px] items-center gap-2">
                <span className={"uppercase tracking-wider " + tone}>{cat}</span>
                <div className="h-1.5 overflow-hidden rounded bg-terminal-border/40">
                  <div
                    className="h-full bg-terminal-amber/70"
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <span className="numeric text-right text-terminal-textBright">{n}</span>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}
