"use client";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/page-header";
import { Panel } from "@/components/panel";
import { CATEGORY_COLOR } from "@/components/markets-coverage-panel";
import { useSharedAgentState } from "@/hooks/use-shared-agent-state";
import { formatCompact, formatUsd, relativeTime } from "@/lib/utils";

interface GammaMarket {
  conditionId?: string;
  condition_id?: string;
  question: string;
  category?: string;
  liquidityNum?: number;
  volumeNum?: number;
  endDateIso?: string;
  active?: boolean;
}

export default function MarketsPage() {
  const { snapshot } = useSharedAgentState();
  const [markets, setMarkets] = useState<GammaMarket[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    // Direct Gamma fetch — agent doesn't expose this yet, so we hit
    // the public API ourselves. Phase 7c moves it behind /api/markets.
    fetch(
      "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100",
    )
      .then((r) => r.json())
      .then((data) => {
        const list = Array.isArray(data) ? data : data?.data ?? [];
        setMarkets(list);
      })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, []);

  const filtered = markets.filter(
    (m) =>
      !filter ||
      (m.question || "").toLowerCase().includes(filter.toLowerCase()) ||
      (m.category || "").toLowerCase().includes(filter.toLowerCase()),
  );

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Markets"
        subtitle={`${snapshot?.markets_watched ?? 0} watched via WS · ${filtered.length} listed`}
        rightSlot={
          <input
            type="text"
            placeholder="filter…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="w-56 rounded border border-terminal-border bg-terminal-bg px-2 py-1 font-mono text-[11px] text-terminal-text placeholder:text-terminal-dim focus:border-terminal-amber focus:outline-none"
          />
        }
      />
      <div className="min-h-0 flex-1 p-2">
        <Panel
          title="Active markets"
          subtitle="Gamma snapshot · top 100 by activity"
          bodyClassName="font-mono text-[11px]"
        >
          {loading && (
            <div className="flex h-full items-center justify-center text-terminal-dim">
              loading markets…
            </div>
          )}
          {err && (
            <div className="border-b border-terminal-red/40 bg-terminal-red/10 px-3 py-2 text-terminal-red">
              {err}
            </div>
          )}
          {!loading && filtered.length === 0 && !err && (
            <div className="flex h-full items-center justify-center text-terminal-dim">
              no markets match filter
            </div>
          )}
          {!loading && filtered.length > 0 && (
            <table className="w-full border-collapse text-left">
              <thead className="sticky top-0 z-10 bg-terminal-surfaceAlt text-[10px] uppercase tracking-wider text-terminal-dim">
                <tr>
                  <th className="px-3 py-1.5 font-medium">Question</th>
                  <th className="px-3 py-1.5 font-medium">Category</th>
                  <th className="px-3 py-1.5 text-right font-medium">Volume</th>
                  <th className="px-3 py-1.5 text-right font-medium">Liquidity</th>
                  <th className="px-3 py-1.5 text-right font-medium">Resolves</th>
                </tr>
              </thead>
              <tbody>
                {filtered.slice(0, 100).map((m, i) => (
                  <Row key={m.conditionId ?? m.condition_id ?? i} m={m} />
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>
    </div>
  );
}

function Row({ m }: { m: GammaMarket }) {
  const cat = m.category ?? "Other";
  const catTone = CATEGORY_COLOR[cat] ?? "text-terminal-amber";
  const vol = m.volumeNum ?? 0;
  const liq = m.liquidityNum ?? 0;
  const end = m.endDateIso ? relativeTime(m.endDateIso) : "—";
  return (
    <tr className="border-t border-terminal-border/60 hover:bg-terminal-surfaceAlt/50">
      <td className="max-w-[600px] truncate px-3 py-1.5 text-terminal-text" title={m.question}>
        {m.question}
      </td>
      <td className="px-3 py-1.5">
        <span
          className={
            "rounded border border-terminal-border px-1.5 py-0.5 text-[10px] uppercase tracking-wider " +
            catTone
          }
        >
          {cat}
        </span>
      </td>
      <td className="numeric px-3 py-1.5 text-right">{formatUsd(vol)}</td>
      <td className="numeric px-3 py-1.5 text-right text-terminal-dim">{formatCompact(liq)}</td>
      <td className="px-3 py-1.5 text-right text-terminal-dim">{end}</td>
    </tr>
  );
}
