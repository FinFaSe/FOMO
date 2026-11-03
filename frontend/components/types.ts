export type Filters = {
  max_entry_market_cap_usd: string;
  min_realized_roi_pct: string;
  min_win_rate: number;
  min_evaluable_positions: number;
  analysis_days: number;
  conservative_win_rate: boolean;
};
export type Wallet = {
  address: string; label: string; favorite: boolean;
  identity: { handle: string; verification: string; source: string } | null;
  sync: { state: string; transactions: number; trades?: number; already_known?: number; rpc_transaction_fetches?: number;
    signature_window_complete: boolean; issues: string[] } | null;
};
export type Fill = {
  signature: string; kind: string; timestamp: string; quantity: string;
  gross_usd: string | null; fees_usd: string | null; market_cap_usd: string | null;
  source_url: string; transaction_url: string; evidence: Record<string, string | number>;
};
export type Position = {
  token: string; cycle: number; opened_at: string; closed_at: string | null;
  status: string; remaining_quantity: string; realized_roi_pct: string | null;
  realized_pnl_usd: string | null; evaluable: boolean; verified: boolean;
  issues: string[]; fills: Fill[];
};
export type Analysis = {
  snapshot_id: string; as_of: string; eligible: boolean; exclusion_reasons: string[];
  metrics: {
    evaluable_positions: number; effective_sample_tokens: number; open_positions: number;
    closed_positions: number; excluded_positions: number; win_rate: number | null;
    win_rate_ci95: [number | null, number | null]; net_realized_pnl_usd: string | null;
    median_roi_pct: string | null; big_winner_rate: number | null; big_winners: number;
    early_big_winners: number; verified_data_fraction: number; top_profit_share: number | null;
  };
  score: {
    historical_skill_provisional: number | null; verified_smart_money: number | null;
    copy_risk: string; components: Record<string, number>; concentration_penalty_fraction: number;
  };
  realized_pnl_curve: {timestamp: string; cumulative_realized_pnl_usd: string}[];
  positions: Position[];
};
export type Observation = {
  signature: string; transaction_url: string; block_time: number | null;
  status: string; slot: number; native_sol_delta: string | null;
  token_deltas: Record<string, string>; classification: string;
};
export type Config = {
  default_filters: Filters; fomo_api_configured: boolean; rpc_source: string; data_mode: string;
  price_source: string | null;
};
export type ChainTrade = {
  signature: string; event_index: number; mint: string; kind: string; block_time: number | null;
  token_amount: string; quote_asset: string | null; quote_amount: string | null; price: string | null;
  network_fee_sol: string; venues: string[]; issues: string[]; transaction_url: string;
  evidence: { trade: string; usd: string; market_cap: string };
  usd?: { gross_usd: string | null; price_usd: string | null; sol_usd: string | null;
    market_cap_usd: string | null; market_cap_method: string | null; quote_method: string };
};
export type ChainPosition = {
  token: string; cycle: number; status: string; opened_at: string; closed_at: string | null;
  remaining_quantity: string; remaining_cost_sol: string | null; realized_cost_sol: string | null; realized_proceeds_sol: string | null;
  realized_pnl_sol: string | null; realized_roi_pct: string | null; avg_entry_price_sol: string | null;
  evaluable: boolean; issues: string[];
};
export type ChainPositions = {
  denomination: string; trades: number; limitations: string[]; positions: ChainPosition[];
  summary: { closed_positions: number; evaluable_positions: number; open_positions: number; wins: number;
    losses: number; win_rate: number | null; net_realized_pnl_sol: string | null;
    aggregate_realized_roi_pct: string | null; median_roi_pct: string | null };
};
export type Job = {
  id: number; wallet: string; state: string; origin: string; round: number; parent_id: number | null;
  attempts: number; run_after: string; created_at: string; started_at: string | null; finished_at: string | null;
  error: string | null; params: { days: number; max_rounds: number };
  progress: { transactions?: number; pages?: number; trades?: number; issues?: string[] } | null;
  result: { state?: string; pricing?: { priced?: number; unpriced?: number; error?: string } } | null;
};
export type WorkerStatus = {
  alive: boolean; queued: number; running: number;
  workers: { id: string; heartbeat_at: string; alive: boolean; info: { refresh_minutes?: number } | null }[];
};
