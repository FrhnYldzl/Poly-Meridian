export type AgentMode = "paper" | "live-conservative" | "live-normal" | "kill";

export interface Position {
  token_id: string;
  qty: number;
  avg_cost: number;
  last_mark: number;
  unrealized_pnl: number;
}

export interface Signal {
  ts: string;
  strategy: string;
  condition_id: string;
  edge: number;
  conviction: number;
  suggested_action: "BUY_YES" | "BUY_NO" | "SELL" | "HOLD" | "EXIT";
  rationale?: Record<string, unknown>;
}

export interface OrderRow {
  ts: string;
  order_id: string;
  strategy: string;
  token_id: string;
  side: "BUY" | "SELL";
  status: string;
  price?: number;
  size?: number;
  filled_size?: number;
  mode: string;
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
}

export type StreamEvent =
  | { type: "snapshot"; data: AgentSnapshot }
  | { type: "signal"; data: Signal }
  | { type: "order"; data: OrderRow }
  | { type: "cluster"; data: SmartMoneyCluster }
  | { type: "kill_switch"; engaged: boolean; reason?: string }
  | { type: "heartbeat"; ts?: string };
