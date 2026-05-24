"use client";

import { useMemo } from "react";
import { PageHeader } from "@/components/page-header";
import { Panel } from "@/components/panel";
import { OrdersFeed } from "@/components/orders-feed";
import { EquityChart } from "@/components/equity-chart";
import { AllocationDonut } from "@/components/allocation-donut";
import { CATEGORY_COLOR } from "@/components/markets-coverage-panel";
import { useSharedAgentState } from "@/hooks/use-shared-agent-state";
import { useNavHistory } from "@/hooks/use-nav-history";
import { cn, formatUsd } from "@/lib/utils";

// Paper starting NAV — matches src/poly_meridian/main.py _build_pipeline_and_news_proc.
const STARTING_NAV_PAPER = 100_000;

export default function PortfolioPage() {
  const { snapshot } = useSharedAgentState();
  const points = useNavHistory(snapshot?.nav_usd);

  const nav = snapshot?.nav_usd ?? 0;
  const cash = snapshot?.cash_usd ?? 0;
  const invested = Math.max(0, nav - cash);
  const dailyPnlPct = snapshot?.daily_pnl_pct ?? 0;
  const positions = snapshot?.open_positions ?? [];
  const orders = snapshot?.last_orders ?? [];
  const startingNav =
    snapshot?.mode === "paper" || !snapshot?.mode ? STARTING_NAV_PAPER : nav;
  const totalReturnPct = startingNav > 0 ? (nav - startingNav) / startingNav : 0;

  // Build allocation slices by category. Cash gets its own slice so the
  // donut sums to NAV — total visible at a glance.
  const slices = useMemo(() => {
    const byCat: Record<string, number> = {};
    for (const p of positions) {
      const cat = (p.category as string | null | undefined) ?? "Other";
      const notional = Math.abs(p.qty * p.last_mark);
      byCat[cat] = (byCat[cat] ?? 0) + notional;
    }
    const out: { label: string; value: number; tone: string }[] = [];
    for (const [cat, val] of Object.entries(byCat)) {
      out.push({
        label: cat,
        value: val,
        tone: CATEGORY_COLOR[cat] ?? "text-terminal-text",
      });
    }
    if (cash > 0) {
      out.push({ label: "Cash", value: cash, tone: "text-terminal-dim" });
    }
    return out;
  }, [positions, cash]);

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Portfolio"
        subtitle={`${snapshot?.mode ?? "paper"} · ${positions.length} open positions · NAV trace`}
      />
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-2 overflow-auto p-2 lg:grid-cols-12">
        {/* Top stat tiles ----------------------------------- */}
        <div className="grid grid-cols-2 gap-2 lg:col-span-12 lg:grid-cols-4">
          <StatTile
            label="NAV"
            value={formatUsd(nav)}
            sub={`vs ${formatUsd(startingNav)} start`}
            tone="text-terminal-textBright"
          />
          <StatTile
            label="Daily P&L"
            value={`${dailyPnlPct >= 0 ? "+" : ""}${(dailyPnlPct * 100).toFixed(2)}%`}
            sub={`${dailyPnlPct >= 0 ? "+" : ""}${formatUsd(nav * dailyPnlPct)}`}
            tone={dailyPnlPct >= 0 ? "text-terminal-green" : "text-terminal-red"}
          />
          <StatTile
            label="Total return"
            value={`${totalReturnPct >= 0 ? "+" : ""}${(totalReturnPct * 100).toFixed(2)}%`}
            sub={`${totalReturnPct >= 0 ? "+" : ""}${formatUsd(nav - startingNav)}`}
            tone={totalReturnPct >= 0 ? "text-terminal-green" : "text-terminal-red"}
          />
          <StatTile
            label="Cash / invested"
            value={`${formatUsd(cash)}`}
            sub={`${formatUsd(invested)} invested · ${((invested / nav) * 100 || 0).toFixed(1)}%`}
            tone="text-terminal-text"
          />
        </div>

        {/* Main row: equity chart + allocation donut --------- */}
        <div className="min-h-0 lg:col-span-8">
          <Panel title="Equity curve" subtitle="rolling NAV · stored in this browser">
            <div className="p-3">
              <EquityChart points={points} startingNav={startingNav} height={320} />
            </div>
          </Panel>
        </div>
        <div className="min-h-0 lg:col-span-4">
          <Panel title="Allocation" subtitle="by category · cash separate">
            <div className="flex h-full items-center justify-center p-3">
              <AllocationDonut
                slices={slices}
                centerValue={formatUsd(nav)}
                centerLabel="NAV"
                size={220}
              />
            </div>
          </Panel>
        </div>

        {/* Bottom row: positions summary + recent orders ----- */}
        <div className="min-h-0 lg:col-span-6">
          <Panel
            title="Open positions"
            subtitle={`${positions.length} active`}
            bodyClassName="font-mono text-[11px]"
          >
            {positions.length === 0 ? (
              <div className="flex h-full items-center justify-center text-terminal-dim">
                no open positions
              </div>
            ) : (
              <ul className="divide-y divide-terminal-border/60">
                {positions.map((p) => (
                  <li
                    key={p.token_id}
                    className="grid grid-cols-[100px_1fr_60px_70px_70px] items-center gap-2 px-3 py-1.5 hover:bg-terminal-surfaceAlt/50"
                  >
                    <span className="text-terminal-amber" title={p.token_id}>
                      {p.token_id.slice(0, 10)}…
                    </span>
                    <span
                      className={cn(
                        "rounded border border-terminal-border px-1.5 py-0.5 text-[10px] uppercase tracking-wider",
                        p.category
                          ? CATEGORY_COLOR[p.category] ?? "text-terminal-text"
                          : "text-terminal-dim",
                      )}
                    >
                      {p.category ?? "—"}
                    </span>
                    <span className="numeric text-right">{p.avg_cost.toFixed(4)}</span>
                    <span className="numeric text-right">{p.last_mark.toFixed(4)}</span>
                    <span
                      className={cn(
                        "numeric text-right",
                        p.unrealized_pnl >= 0 ? "text-terminal-green" : "text-terminal-red",
                      )}
                    >
                      {formatUsd(p.unrealized_pnl, { showSign: true })}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </div>
        <div className="min-h-0 lg:col-span-6">
          <OrdersFeed orders={orders} />
        </div>
      </div>
    </div>
  );
}

function StatTile({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: string;
}) {
  return (
    <div className="rounded border border-terminal-border bg-terminal-surface p-3">
      <div className="text-[10px] uppercase tracking-wider text-terminal-dim">{label}</div>
      <div className={cn("numeric mt-1 text-xl font-semibold", tone ?? "text-terminal-text")}>
        {value}
      </div>
      {sub && <div className="mt-0.5 text-[10px] text-terminal-dim">{sub}</div>}
    </div>
  );
}
