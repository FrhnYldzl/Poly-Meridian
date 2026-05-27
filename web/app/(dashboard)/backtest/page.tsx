"use client";

import { useEffect, useState } from "react";
import { PageHeader } from "@/components/page-header";
import { Panel } from "@/components/panel";
import { EquityChart } from "@/components/equity-chart";
import { cn, formatUsd } from "@/lib/utils";

// Phase S.1 — LLM calibration backtest result shape
interface LLMCalibrationResult {
  started_at?: string;
  finished_at?: string;
  n_markets_attempted?: number;
  n_markets_scored?: number;
  n_skipped_voided?: number;
  n_skipped_no_estimate?: number;
  brier_score?: number | null;
  accuracy?: number | null;
  mean_confidence?: number | null;
  mean_p_long?: number | null;
  cost_usd_estimate?: number;
  bucket_accuracy?: Record<string, number | null>;
  bucket_counts?: Record<string, number>;
  sample_predictions?: Array<{
    question: string;
    category?: string;
    claimed_p_long: number;
    confidence: number;
    won: boolean;
    rationale: string;
  }>;
  never_run?: boolean;
  error?: string;
}

const API = process.env.NEXT_PUBLIC_API_URL ?? "";

interface BacktestTrade {
  ts: string;
  market: string;
  side: string;
  entry_price: number;
  exit_price: number;
  pnl_usd: number;
}

interface BacktestResult {
  mode: string;
  config: {
    seed: number;
    n_markets: number;
    n_steps: number;
    step_sec: number;
    starting_nav: number;
    bet_size_pct: number;
    zscore_threshold: number;
  };
  equity_curve: { ts: string; nav: number }[];
  trades: BacktestTrade[];
  total_return_pct: number;
  sharpe: number;
  max_drawdown_pct: number;
  win_rate: number;
  trade_count: number;
  final_nav: number;
  starting_nav: number;
  duration_sec: number;
}

export default function BacktestPage() {
  const [seed, setSeed] = useState(42);
  const [steps, setSteps] = useState(200);
  const [markets, setMarkets] = useState(12);
  const [zscore, setZscore] = useState(1.5);
  const [betSizePct, setBetSizePct] = useState(2.0);  // shown as %
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Phase S.1 — LLM calibration backtest state
  const [llmN, setLlmN] = useState(30);
  const [llmRunning, setLlmRunning] = useState(false);
  const [llmResult, setLlmResult] = useState<LLMCalibrationResult | null>(null);
  const [llmErr, setLlmErr] = useState<string | null>(null);

  // Load cached previous LLM backtest on mount
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`${API}/api/backtest/llm-calibration/last`);
        const j = (await r.json()) as LLMCalibrationResult;
        if (!cancelled && !j.never_run) setLlmResult(j);
      } catch {
        /* ignore */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const onRunLLM = async () => {
    setLlmRunning(true);
    setLlmErr(null);
    try {
      const params = new URLSearchParams({ n_markets: String(llmN) });
      const r = await fetch(
        `${API}/api/backtest/llm-calibration?${params}`,
        { method: "POST" },
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = (await r.json()) as LLMCalibrationResult;
      if (j.error) setLlmErr(j.error);
      else setLlmResult(j);
    } catch (e) {
      setLlmErr(String(e));
    } finally {
      setLlmRunning(false);
    }
  };

  const onRun = async () => {
    setRunning(true);
    setErr(null);
    try {
      const params = new URLSearchParams({
        seed: String(seed),
        n_markets: String(markets),
        n_steps: String(steps),
        zscore_threshold: String(zscore),
        bet_size_pct: String(betSizePct / 100),
      });
      const r = await fetch(`${API}/api/backtest/run?${params}`, {
        method: "POST",
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j: BacktestResult = await r.json();
      setResult(j);
    } catch (e) {
      setErr(String(e));
    } finally {
      setRunning(false);
    }
  };

  // Convert equity curve to NavPoint[] shape EquityChart expects.
  const navPoints =
    result?.equity_curve.map((p) => ({
      ts: new Date(p.ts).getTime(),
      nav: p.nav,
    })) ?? [];

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Backtest"
        subtitle={
          result
            ? `${result.mode} · ${result.trade_count} trades · seed ${result.config.seed}`
            : "deterministic synthetic backtest · click Run"
        }
        badge="MVP"
      />

      {/* Controls row */}
      <div className="grid grid-cols-2 gap-2 border-b border-terminal-border bg-terminal-surfaceAlt/40 p-3 sm:grid-cols-3 lg:grid-cols-6">
        <Input
          label="Seed"
          value={seed}
          onChange={(v) => setSeed(parseInt(v) || 0)}
        />
        <Input
          label="Markets"
          value={markets}
          onChange={(v) => setMarkets(Math.max(1, Math.min(50, parseInt(v) || 1)))}
        />
        <Input
          label="Steps"
          value={steps}
          onChange={(v) => setSteps(Math.max(20, Math.min(2000, parseInt(v) || 20)))}
        />
        <Input
          label="Zscore thr."
          value={zscore}
          step={0.1}
          onChange={(v) => setZscore(Math.max(0.5, Math.min(4, parseFloat(v) || 1.5)))}
        />
        <Input
          label="Bet size %"
          value={betSizePct}
          step={0.5}
          onChange={(v) => setBetSizePct(Math.max(0.1, Math.min(10, parseFloat(v) || 2)))}
        />
        <div className="flex items-end">
          <button
            type="button"
            onClick={onRun}
            disabled={running}
            className={cn(
              "h-[34px] w-full rounded border px-3 font-mono text-[12px] uppercase tracking-wider transition-colors",
              running
                ? "border-terminal-dim bg-terminal-bg text-terminal-dim"
                : "border-terminal-amber/60 bg-terminal-amber/10 text-terminal-amber hover:bg-terminal-amber/20",
            )}
          >
            {running ? "Running…" : "▶ Run"}
          </button>
        </div>
      </div>

      {err && (
        <div className="border-b border-terminal-red/40 bg-terminal-red/10 px-3 py-2 font-mono text-[11px] text-terminal-red">
          {err}
        </div>
      )}

      {/* Phase S.1 — LLM Calibration backtest. Validates Claude's forecast
          skill on past resolved Polymarket markets BEFORE we trust live
          capital. Cheap (~$0.02 per 50-market run). The Brier score and
          per-bucket accuracy are the actual scientific proof that this
          system has edge. */}
      <div className="border-b border-terminal-border bg-terminal-surfaceAlt/20 p-3">
        <div className="mb-2 flex items-baseline justify-between">
          <div>
            <div className="font-mono text-[13px] font-semibold uppercase tracking-wider text-terminal-green">
              LLM Calibration Backtest
            </div>
            <div className="font-mono text-[10px] text-terminal-dim">
              Score Claude on past resolved markets — Brier &lt; 0.20 ≈ informed,
              0.25 ≈ coin-flip, &gt; 0.30 ≈ worse than chance
            </div>
          </div>
          <div className="flex items-end gap-2">
            <Input
              label="Markets"
              value={llmN}
              onChange={(v) =>
                setLlmN(Math.max(5, Math.min(200, parseInt(v) || 30)))
              }
            />
            <button
              type="button"
              onClick={onRunLLM}
              disabled={llmRunning}
              className={cn(
                "h-[34px] rounded border px-3 font-mono text-[12px] uppercase tracking-wider transition-colors",
                llmRunning
                  ? "border-terminal-dim bg-terminal-bg text-terminal-dim"
                  : "border-terminal-green/60 bg-terminal-green/10 text-terminal-green hover:bg-terminal-green/20",
              )}
            >
              {llmRunning ? "Querying Claude…" : "▶ Validate LLM"}
            </button>
          </div>
        </div>

        {llmErr && (
          <div className="my-2 rounded border border-terminal-red/40 bg-terminal-red/10 px-3 py-2 font-mono text-[11px] text-terminal-red">
            {llmErr}
          </div>
        )}

        {llmResult && llmResult.n_markets_scored !== undefined && llmResult.n_markets_scored > 0 && (
          <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
            <StatTile
              label="Brier score"
              value={
                llmResult.brier_score != null
                  ? llmResult.brier_score.toFixed(4)
                  : "—"
              }
              tone={
                llmResult.brier_score == null
                  ? "text-terminal-dim"
                  : llmResult.brier_score < 0.20
                    ? "text-terminal-green"
                    : llmResult.brier_score < 0.25
                      ? "text-terminal-amber"
                      : "text-terminal-red"
              }
              sub={
                llmResult.brier_score != null && llmResult.brier_score < 0.20
                  ? "informed"
                  : llmResult.brier_score != null && llmResult.brier_score < 0.25
                    ? "weak signal"
                    : "no skill"
              }
            />
            <StatTile
              label="Accuracy"
              value={
                llmResult.accuracy != null
                  ? `${(llmResult.accuracy * 100).toFixed(1)}%`
                  : "—"
              }
              sub={`${llmResult.n_markets_scored}/${llmResult.n_markets_attempted} scored`}
            />
            <StatTile
              label="Mean confidence"
              value={
                llmResult.mean_confidence != null
                  ? `${(llmResult.mean_confidence * 100).toFixed(1)}%`
                  : "—"
              }
              sub={
                llmResult.mean_confidence != null && llmResult.accuracy != null
                  ? `${(llmResult.accuracy * 100).toFixed(0)}% realized`
                  : ""
              }
            />
            <StatTile
              label="Mean p_long"
              value={
                llmResult.mean_p_long != null
                  ? llmResult.mean_p_long.toFixed(3)
                  : "—"
              }
              sub="bet-side prob"
            />
            <StatTile
              label="Cost"
              value={`$${(llmResult.cost_usd_estimate ?? 0).toFixed(4)}`}
              sub={`${llmResult.n_skipped_no_estimate ?? 0} no-est, ${llmResult.n_skipped_voided ?? 0} void`}
            />
          </div>
        )}

        {/* Per-bucket calibration table */}
        {llmResult?.bucket_counts && Object.keys(llmResult.bucket_counts).length > 0 && (
          <div className="mt-2 grid grid-cols-5 gap-1.5 font-mono text-[10px]">
            {Object.entries(llmResult.bucket_counts).map(([bucket, n]) => {
              const acc = llmResult.bucket_accuracy?.[bucket];
              const expected = parseFloat(bucket.split("-")[0]) + 0.05;
              const gap =
                acc != null ? Math.abs(acc - expected) : null;
              const tone =
                gap == null
                  ? "text-terminal-dim"
                  : gap < 0.05
                    ? "text-terminal-green"
                    : gap < 0.15
                      ? "text-terminal-amber"
                      : "text-terminal-red";
              return (
                <div
                  key={bucket}
                  className="rounded border border-terminal-border bg-terminal-bg px-1.5 py-1"
                >
                  <div className="text-terminal-dim">{bucket}</div>
                  <div className={cn("numeric", tone)}>
                    {acc != null ? `${(acc * 100).toFixed(0)}% (n=${n})` : `n=${n}`}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* Sample predictions — operator can see what Claude actually said */}
        {llmResult?.sample_predictions && llmResult.sample_predictions.length > 0 && (
          <details className="mt-2">
            <summary className="cursor-pointer font-mono text-[10px] uppercase tracking-wider text-terminal-dim hover:text-terminal-text">
              ▶ Sample predictions ({llmResult.sample_predictions.length} of {llmResult.n_markets_scored})
            </summary>
            <ul className="mt-1 max-h-[300px] divide-y divide-terminal-border/40 overflow-auto rounded border border-terminal-border bg-terminal-bg font-mono text-[10px]">
              {llmResult.sample_predictions.map((p, i) => (
                <li
                  key={i}
                  className={cn(
                    "px-2 py-1.5",
                    p.won ? "border-l-2 border-terminal-green" : "border-l-2 border-terminal-red",
                  )}
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="truncate text-terminal-text">{p.question}</span>
                    <span
                      className={cn(
                        "numeric whitespace-nowrap",
                        p.won ? "text-terminal-green" : "text-terminal-red",
                      )}
                    >
                      p={p.claimed_p_long.toFixed(2)} {p.won ? "✓" : "✗"}
                    </span>
                  </div>
                  {p.rationale && (
                    <div className="mt-0.5 truncate text-terminal-dim">
                      Claude: {p.rationale}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>

      {/* Results */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-2 overflow-auto p-2 lg:grid-cols-12">
        {/* Stat tiles */}
        <div className="grid grid-cols-2 gap-2 lg:col-span-12 lg:grid-cols-4">
          <StatTile
            label="Total return"
            value={
              result
                ? `${result.total_return_pct >= 0 ? "+" : ""}${(result.total_return_pct * 100).toFixed(2)}%`
                : "—"
            }
            tone={
              result
                ? result.total_return_pct >= 0
                  ? "text-terminal-green"
                  : "text-terminal-red"
                : "text-terminal-dim"
            }
            sub={result ? `${formatUsd(result.final_nav)} final` : ""}
          />
          <StatTile
            label="Sharpe"
            value={result ? result.sharpe.toFixed(2) : "—"}
            tone={
              result
                ? result.sharpe >= 1
                  ? "text-terminal-green"
                  : result.sharpe < 0
                    ? "text-terminal-red"
                    : "text-terminal-amber"
                : "text-terminal-dim"
            }
            sub={result ? "annualized" : ""}
          />
          <StatTile
            label="Max drawdown"
            value={result ? `${(result.max_drawdown_pct * 100).toFixed(2)}%` : "—"}
            tone={
              result
                ? result.max_drawdown_pct < 0.05
                  ? "text-terminal-green"
                  : result.max_drawdown_pct < 0.15
                    ? "text-terminal-amber"
                    : "text-terminal-red"
                : "text-terminal-dim"
            }
            sub={result ? "peak-to-trough" : ""}
          />
          <StatTile
            label="Win rate"
            value={result ? `${(result.win_rate * 100).toFixed(1)}%` : "—"}
            tone={
              result
                ? result.win_rate >= 0.55
                  ? "text-terminal-green"
                  : "text-terminal-amber"
                : "text-terminal-dim"
            }
            sub={result ? `${result.trade_count} trades` : ""}
          />
        </div>

        {/* Equity curve */}
        <div className="min-h-0 lg:col-span-8">
          <Panel
            title="Equity curve"
            subtitle={
              result
                ? `${result.equity_curve.length} points · step ${result.config.step_sec}s`
                : "run a backtest to see equity curve"
            }
          >
            <div className="p-3">
              {result ? (
                <EquityChart
                  points={navPoints}
                  startingNav={result.starting_nav}
                  height={360}
                />
              ) : (
                <div
                  className="grid-bg flex items-center justify-center rounded border border-terminal-border bg-terminal-surface text-[11px] uppercase tracking-wider text-terminal-dim"
                  style={{ height: 360 }}
                >
                  no results yet
                </div>
              )}
            </div>
          </Panel>
        </div>

        {/* Trade list */}
        <div className="min-h-0 lg:col-span-4">
          <Panel
            title="Trades"
            subtitle={result ? `${result.trades.length} executed` : ""}
            bodyClassName="font-mono text-[10px]"
          >
            {!result ? (
              <div className="flex h-full items-center justify-center text-terminal-dim">
                ↩ run to see trades
              </div>
            ) : result.trades.length === 0 ? (
              <div className="flex h-full items-center justify-center text-terminal-dim">
                no trades — try lower zscore
              </div>
            ) : (
              <ul className="divide-y divide-terminal-border/60">
                {result.trades.slice(-50).reverse().map((t, i) => (
                  <li
                    key={i}
                    className="grid grid-cols-[1fr_70px_70px_70px] items-center gap-2 px-3 py-1"
                  >
                    <span className="truncate text-terminal-amber">
                      {t.market}
                    </span>
                    <span
                      className={
                        t.side === "BUY_YES"
                          ? "text-terminal-green"
                          : "text-terminal-red"
                      }
                    >
                      {t.side}
                    </span>
                    <span className="numeric text-right text-terminal-dim">
                      {t.entry_price.toFixed(3)}→{t.exit_price.toFixed(3)}
                    </span>
                    <span
                      className={cn(
                        "numeric text-right",
                        t.pnl_usd >= 0 ? "text-terminal-green" : "text-terminal-red",
                      )}
                    >
                      {formatUsd(t.pnl_usd, { showSign: true })}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
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
      <div className="text-[10px] uppercase tracking-wider text-terminal-dim">
        {label}
      </div>
      <div className={cn("numeric mt-1 text-xl font-semibold", tone)}>{value}</div>
      {sub && <div className="mt-0.5 text-[10px] text-terminal-dim">{sub}</div>}
    </div>
  );
}

function Input({
  label,
  value,
  onChange,
  step,
}: {
  label: string;
  value: number;
  onChange: (v: string) => void;
  step?: number;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] uppercase tracking-wider text-terminal-dim">
        {label}
      </span>
      <input
        type="number"
        value={value}
        step={step ?? 1}
        onChange={(e) => onChange(e.target.value)}
        className="h-[34px] rounded border border-terminal-border bg-terminal-bg px-2 font-mono text-[12px] text-terminal-text focus:border-terminal-amber focus:outline-none"
      />
    </label>
  );
}
