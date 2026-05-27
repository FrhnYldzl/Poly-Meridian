export type AgentMode = "paper" | "live-conservative" | "live-normal" | "kill";

export interface PositionEntry {
  strategy: string;
  entry_price: number;
  entry_ts: string;
}

export interface TradeMetrics {
  entry_price: number;
  size_units: number;
  notional_usd: number;
  max_loss_usd: number;
  max_gain_usd: number;
  risk_reward_ratio: number;
  our_prob: number;
  expected_pnl_usd: number;
  ev_per_dollar: number;
}

export interface Position {
  token_id: string;
  qty: number;
  avg_cost: number;
  last_mark: number;
  unrealized_pnl: number;
  entry?: PositionEntry | null;
  trade_metrics?: TradeMetrics | null;
  category?: string | null;
  days_to_resolution?: number | null;
  end_date_iso?: string | null;
  // Market metadata so the row is human-readable.
  question?: string | null;
  outcome?: "Yes" | "No" | null;
  condition_id?: string | null;
  polymarket_url?: string | null;
}

export interface Signal {
  ts: string;
  strategy: string;
  condition_id: string;
  token_id?: string;
  edge: number;
  conviction: number;
  suggested_action: "BUY_YES" | "BUY_NO" | "SELL" | "HOLD" | "EXIT";
  rationale?: Record<string, unknown>;
  market_question?: string;
}

export interface OrderRow {
  ts: string;
  order_id: string;
  strategy: string;
  contributors?: string[];
  condition_id?: string;
  token_id: string;
  side: "BUY" | "SELL";
  status: string;
  price?: number;
  size?: number;
  filled_size?: number;
  avg_fill_price?: number | null;
  mode: string;
  edge?: number;
  conviction?: number;
  size_pct?: number;
  market_question?: string;
  category?: string | null;
  trade_metrics?: TradeMetrics | null;
}

export interface SmartMoneyCluster {
  condition_id: string;
  direction: "YES" | "NO";
  cluster_size: number;
  tier: number;
  net_usd_total: number;
  ts: string;
  wallets?: string[];
}

export interface AgentSnapshot {
  ts: string;
  mode: AgentMode;
  nav_usd: number;
  cash_usd: number;
  open_positions: Position[];
  open_position_count: number;
  daily_pnl_pct: number;
  total_exposure_pct: number;
  kill_switch_engaged: boolean;
  kill_switch_reason: string | null;
  strategies_enabled: string[];
  last_orders: OrderRow[];
  last_signals: Signal[];
  smart_money_clusters: SmartMoneyCluster[];
  markets_watched: number;
  uptime_sec: number;
  pipeline_ticks_total?: number;
  strategies_evaluated_total?: number;
  arb_opportunities_total?: number;
  last_book_update_ts?: string | null;
  db_ok?: boolean;
  cache_ok?: boolean;
  sentiment_enabled?: boolean;
  onchain_enabled?: boolean;
  // News pipeline funnel: GDELT articles → matched → scored → signals emitted.
  news_ingested_total?: number;
  news_processed_total?: number;
  news_signals_emitted_total?: number;
  news_matcher_mode?: "inmem-vector" | "pgvector" | "keyword" | null;
  scorer_kind?: "claude" | "gemini" | "heuristic" | null;
  // Polymarket category coverage — counts of active markets we know about.
  markets_by_category?: Record<string, number>;
  markets_active_total?: number;
  ws_subscribed_total?: number;
  // Trade-flow funnel: signal → aggregator → risk → order.
  signals_emitted_total?: number;
  signals_aggregated_total?: number;
  risk_accepted_total?: number;
  risk_rejected_total?: number;
  orders_submitted_total?: number;
  // Resolution-aware NAV split (Phase I.0).
  //   liquidation_nav_usd — what we'd get if we exited every position now
  //   thesis_nav_usd      — what NAV would be if positions revert to entry
  liquidation_nav_usd?: number;
  thesis_nav_usd?: number;
  // Genesis NAV — Ledger.starting_cash, surfaced so "vs start" math works
  // when PAPER_STARTING_NAV overrides the $100K default.
  starting_nav_usd?: number;
  // Phase Q.1 — per-(strategy, reason) reject counts.
  //   { "arbitrage": { "no_arb_gap": 1234, "no_book": 5 }, ... }
  // Surfaces WHY each strategy returned None, so the operator can see
  // which constraint is killing trades.
  strategy_rejects?: Record<string, Record<string, number>>;
  // Phase R.6 — LLM resolver usage. Tracks Claude API spend, cache
  // hit rate, model tier mix.
  llm_usage?: {
    llm_enabled?: boolean;
    calls_total?: number;
    calls_triage?: number;
    calls_deep?: number;
    calls_cache_hits?: number;
    calls_budget_blocked?: number;
    tokens_input?: number;
    tokens_output?: number;
    spend_usd_today?: number;
    last_reset_date?: string;
  };
  // Phase K — slippage drift summary (median realized vs predicted).
  slippage_summary?: Record<string, unknown>;
  // Diagnostic — last broker_refresh_loop exception, empty when healthy.
  last_refresh_error?: string | null;
}

export type StreamEvent =
  | { type: "snapshot"; data: AgentSnapshot }
  | { type: "signal"; data: Signal }
  | { type: "order"; data: OrderRow }
  | { type: "cluster"; data: SmartMoneyCluster }
  | { type: "kill_switch"; engaged: boolean; reason?: string }
  | { type: "heartbeat"; ts?: string };
