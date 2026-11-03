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
  realized_pnl_usd: string | null; evaluable: boolean; verified: boolean; dust_written_off_quantity?: string;
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
  behavior?: { classification: string; same_block_round_trips: number; fast_round_trips: number;
    fast_round_trip_share: number; max_trades_per_day: number; active_days: number };
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
  alive: boolean; queued: number; running: number; watcher_alive?: boolean;
  watchers?: { id: string; heartbeat_at: string; alive: boolean; info: { interval_seconds?: number; watching?: number } | null }[];
  workers: { id: string; heartbeat_at: string; alive: boolean; info: { refresh_minutes?: number } | null }[];
};
export type OpenPosition = {
  token: string; cycle: number; opened_at: string; symbol: string | null; quantity: string;
  cost_basis_usd: string | null; price_usd: string | null; value_usd: string | null;
  unrealized_pnl_usd: string | null; unrealized_pct: string | null; liquidity_usd: string | null;
  current_market_cap_usd: string | null; dex_id: string | null; quote_fetched_at: string | null;
  quote_found: boolean; issues: string[]; risks: string[];
};
export type OpenPositions = {
  as_of: string; positions: OpenPosition[]; issues: string[]; quote_source: string | null;
  totals: { open_positions: number; valued_positions: number; value_usd: string; cost_basis_usd: string;
    unrealized_pnl_usd: string };
};
export type Seed = {
  mint: string; label: string; scan_address: string; address_kind: string; state: string; pages: number;
  launch_time: string | null; creator: string | null; error: string | null; declared_peak_cap_usd: string | null;
  outcome: { pump_origin: boolean; launch_cap_usd: string | null; peak_cap_lower_bound_usd: string | null;
    peak_basis: string | null; peak_multiple_from_launch: string | null; early_buyers: number; issues: string[] } | null;
};
export type Candidate = {
  wallet: string; seeds: number; hits: number; misses: number; unknown: number; hit_rate: number | null;
  hit_rate_ci95: [number | null, number | null]; declared_peak_hits: number; median_minutes_after_launch: number | null;
  median_rank: number; flags: string[]; tracked: boolean;
  median_price_move_30s: string | null; median_size_vs_curve: string | null; entity: { id: string; size: number } | null;
  entries: { mint: string; label: string; rank: number; status: string; multiple: string | null; entry_cap_usd: string | null }[];
};
export type Funding = {
  address: string; state: string; funder: string | null; funder_kind: string | null; reason: string | null;
  lamports: string | null; block_time: string | null;
};
export type WalletLinks = {
  funding: Funding | null; funded: Funding[];
  entity: { id: string; size: number; members: string[]; links: { a: string; b: string; reason: string }[] } | null;
};
export type AlertItem = {
  id: number; kind: string; wallet: string | null; mint: string; signature: string | null; event_time: string | null;
  created_at: string; detection_seconds: number | null; alert_seconds: number | null; transaction_url: string | null;
  read: boolean; delivery: { state: string; attempts: number; error: string | null };
  details: { label?: string; symbol?: string | null; usd?: string | null; quote_amount?: string | null; quote_asset?: string | null;
    market_cap_usd?: string | null; market_cap_status?: string; venues?: string[]; entities?: number; window_minutes?: number;
    participants?: { wallet: string; label: string; block_time: string; usd: string | null }[] };
};
export type AlertList = {
  items: AlertItem[]; unread: number; webhook_configured: boolean;
  settings: { max_age_minutes: number; convergence_min: number; convergence_minutes: number };
};
export type BacktestRow = {
  delay_seconds: number; buy_signals: number; filled_buys: number; unfillable_buys: number; mirrored_sells: number;
  unfillable_sells: number; closed_positions: number; wins: number; losses: number; win_rate: number | null;
  invested_sol: string; realized_pnl_sol: string; roi_pct: string | null; leader_roi_pct: string | null;
  copy_gap_pct: string | null; open_positions: number; open_cost_sol: string; open_mark_value_sol: string | null;
  fills_chain?: number; fills_candle?: number;
};
export type BacktestResult = {
  leader_trades: number; observations: number; candles?: number; results: BacktestRow[]; limitations: string[];
  params: { size_sol: string; slippage_bps: string; max_staleness_seconds: number };
};
export type HorizonSummary = { n: number; median_pct: string | null; mean_pct: string | null; hit_rate: number | null };
export type ProspectiveReport = {
  buy_alerts_with_entry_quote: number; median_quote_lateness_seconds: number | null; notice: string;
  horizons: Record<string, HorizonSummary>;
  wallets: ({ wallet: string; label: string } & Record<string, HorizonSummary>)[];
};
export type ChartData = {
  mint: string; from: number; to: number; timeframe: string | null; quote: string | null; notice: string;
  candles: { ts: number; open: string; high: string; low: string; close: string }[];
  points: { ts: number; price_sol: string; side: string }[];
  trades: { ts: number; kind: string; price_sol: string | null; quote_asset: string | null; token_amount: string; signature: string }[];
  histories: { id: number; timeframe: string; state: string; candles: number; error: string | null }[];
};
