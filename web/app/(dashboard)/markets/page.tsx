"use client";

import { useEffect, useMemo, useState } from "react";
import { PageHeader } from "@/components/page-header";
import { Panel } from "@/components/panel";
import { CATEGORY_COLOR } from "@/components/markets-coverage-panel";
import { useSharedAgentState } from "@/hooks/use-shared-agent-state";
import { formatCompact, formatUsd, relativeTime } from "@/lib/utils";

// Empty string → same-origin (FastAPI serves both UI + API in prod).
const API = process.env.NEXT_PUBLIC_API_URL ?? "";

interface MarketRow {
  condition_id: string;
  question: string;
  category: string;
  liquidity: number;
  volume: number;
  end_date: string | null;
  active: boolean;
  closed: boolean;
}

interface MarketsResp {
  markets: MarketRow[];
  total: number;
  universe_total: number;
  by_category: Record<string, number>;
}

type SortKey = "liquidity" | "volume" | "end_date";

const SORT_LABEL: Record<SortKey, string> = {
  liquidity: "Liquidity",
  volume: "Volume",
  end_date: "Resolves",
};

export default function MarketsPage() {
  const { snapshot } = useSharedAgentState();
  const [data, setData] = useState<MarketsResp | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [category, setCategory] = useState<string>("all");
  const [sort, setSort] = useState<SortKey>("liquidity");

  // Fetch full markets directory from the agent (not raw Gamma). The agent
  // pre-fetches all active markets each gamma_sync cycle (5 min) and attaches
  // derived categories, so this call is cheap and category-correct.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const params = new URLSearchParams();
    if (category !== "all") params.set("category", category);
    params.set("sort", sort);
    params.set("limit", "2000");
    fetch(`${API}/api/markets?${params.toString()}`)
      .then((r) => r.json())
      .then((j: MarketsResp) => {
        if (!cancelled) setData(j);
      })
      .catch((e) => {
        if (!cancelled) setErr(String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [category, sort]);

  // Text filter runs client-side over the already-fetched rows.
  const filtered = useMemo(() => {
    if (!data?.markets) return [];
    if (!filter) return data.markets;
    const lc = filter.toLowerCase();
    return data.markets.filter(
      (m) =>
        (m.question || "").toLowerCase().includes(lc) ||
        (m.category || "").toLowerCase().includes(lc),
    );
  }, [data, filter]);

  const categoryOrder = useMemo(() => {
    if (!data?.by_category) return [] as string[];
    return Object.entries(data.by_category)
      .sort(([, a], [, b]) => b - a)
      .map(([cat]) => cat);
  }, [data?.by_category]);

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Markets"
        subtitle={
          data
            ? `${data.universe_total.toLocaleString()} active · ${snapshot?.ws_subscribed_total ?? 0} on WS · ${filtered.length} shown`
            : "loading…"
        }
        rightSlot={
          <input
            type="text"
            placeholder="filter question…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="w-56 rounded border border-terminal-border bg-terminal-bg px-2 py-1 font-mono text-[11px] text-terminal-text placeholder:text-terminal-dim focus:border-terminal-amber focus:outline-none"
          />
        }
      />

      {/* Filter chips + sort row */}
      <div className="flex flex-wrap items-center gap-1 border-b border-terminal-border bg-terminal-surfaceAlt/40 px-2 py-1.5">
        <Chip
          label="All"
          count={data?.universe_total ?? 0}
          active={category === "all"}
          tone="text-terminal-amber"
          onClick={() => setCategory("all")}
        />
        {categoryOrder.map((cat) => (
          <Chip
            key={cat}
            label={cat}
            count={data?.by_category[cat] ?? 0}
            active={category === cat}
            tone={CATEGORY_COLOR[cat] ?? "text-terminal-text"}
            onClick={() => setCategory(cat)}
          />
        ))}
        <span className="ml-auto flex items-center gap-1 text-[10px] uppercase tracking-wider text-terminal-dim">
          Sort:
          {(["liquidity", "volume", "end_date"] as const).map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => setSort(k)}
              className={
                "rounded border px-1.5 py-0.5 transition-colors " +
                (sort === k
                  ? "border-terminal-amber/50 bg-terminal-amber/10 text-terminal-amber"
                  : "border-terminal-border text-terminal-dim hover:text-terminal-text")
              }
            >
              {SORT_LABEL[k]}
            </button>
          ))}
        </span>
      </div>

      <div className="min-h-0 flex-1 p-2">
        <Panel
          title="Active markets"
          subtitle={`live snapshot · ${SORT_LABEL[sort].toLowerCase()} sort`}
          bodyClassName="font-mono text-[11px]"
        >
          {loading && !data && (
            <div className="flex h-full items-center justify-center text-terminal-dim">
              loading markets directory…
            </div>
          )}
          {err && (
            <div className="border-b border-terminal-red/40 bg-terminal-red/10 px-3 py-2 text-terminal-red">
              {err}
            </div>
          )}
          {data && !err && filtered.length === 0 && (
            <div className="flex h-full items-center justify-center text-terminal-dim">
              no markets match filter
            </div>
          )}
          {data && filtered.length > 0 && (
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
                {filtered.map((m, i) => (
                  <Row key={m.condition_id ?? i} m={m} />
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>
    </div>
  );
}

function Chip({
  label,
  count,
  active,
  tone,
  onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  tone: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        "flex items-center gap-1 rounded border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider transition-colors " +
        (active
          ? "border-terminal-amber/60 bg-terminal-amber/10 text-terminal-amber"
          : "border-terminal-border text-terminal-dim hover:border-terminal-amber/30 hover:text-terminal-text")
      }
    >
      <span className={active ? "" : tone}>{label}</span>
      <span className="numeric text-[9px] text-terminal-dim/80">{count}</span>
    </button>
  );
}

function Row({ m }: { m: MarketRow }) {
  const cat = m.category ?? "Other";
  const catTone = CATEGORY_COLOR[cat] ?? "text-terminal-amber";
  const end = m.end_date ? relativeTime(m.end_date) : "—";
  return (
    <tr className="border-t border-terminal-border/60 hover:bg-terminal-surfaceAlt/50">
      <td
        className="max-w-[600px] truncate px-3 py-1.5 text-terminal-text"
        title={m.question}
      >
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
      <td className="numeric px-3 py-1.5 text-right">{formatUsd(m.volume)}</td>
      <td className="numeric px-3 py-1.5 text-right text-terminal-dim">
        {formatCompact(m.liquidity)}
      </td>
      <td className="px-3 py-1.5 text-right text-terminal-dim">{end}</td>
    </tr>
  );
}
