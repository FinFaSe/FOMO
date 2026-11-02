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
  sync: { state: string; transactions: number; signature_window_complete: boolean; issues: string[] } | null;
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
};
