/** API client for the Webull Trading Bot backend. */

const BASE = '';

async function fetchJson<T>(url: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${url}`, opts);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

function post<T>(url: string, body?: unknown): Promise<T> {
  return fetchJson<T>(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
}

function del<T>(url: string): Promise<T> {
  return fetchJson<T>(url, { method: 'DELETE' });
}

// --- Types ---

export interface Status {
  server: string;
  version: string;
  mode: string;
  broker_provider: string;
  market_data_provider: string;
  authenticated: boolean;
  account_type: string;
  time_et: string;
  trading_phase: string;
}

export interface Position {
  symbol: string;
  qty: number;
  avg_cost: number;
  market_value: number;
  unrealized_pnl: number;
  current_price: number;
}

export interface AccountInfo {
  account_id: string;
  net_liquidation: number;
  buying_power: number;
  cash: number;
  day_trades_remaining: number;
  account_type: string;
}

export interface ScalperStatus {
  enabled: boolean;
  running: boolean;
  open_positions: number;
  session_trades: number;
  kill_switch: boolean;
  config: Record<string, unknown>;
}

export interface Trade {
  symbol: string;
  side: string;
  qty: number;
  entry_price: number;
  hold_seconds: number;
  high_since_entry: number;
  order_id: string;
}

export interface WorklistEntry {
  symbol: string;
  score: number;
  added_at: number;
  last_scored_at: number;
  source: string;
  age_seconds: number;
}

export interface WatchlistEntry {
  symbol: string;
  state: string;
  score: number;
  time_in_state: number;
}

export interface QuoteData {
  symbol: string;
  price: number;
  bid: number;
  ask: number;
  spread: number;
  spread_pct: number;
  volume: number;
  change_pct: number;
}

export interface EODReport {
  date: string;
  total_trades: number;
  winners: number;
  losers: number;
  win_rate: number;
  total_pnl: number;
  gross_profit: number;
  gross_loss: number;
  profit_factor: number | string;
  avg_hold_seconds: number;
  exit_reasons: Record<string, number>;
  symbols_traded: string[];
  blocked_trades: number;
  shadow_trades: number;
  total_events: number;
  message?: string;
}

export interface WatchdogStatus {
  running: boolean;
  last_heartbeat_age: number;
  recent_alerts: Array<{ type: string; message: string; timestamp_et: string }>;
  total_alerts: number;
}

// --- API calls ---

export const api = {
  health: () => fetchJson<{ status: string; timestamp: string }>('/api/health'),
  status: () => fetchJson<Status>('/api/status'),
  account: () => fetchJson<AccountInfo>('/api/account'),
  positions: () => fetchJson<{ positions: Position[]; count: number }>('/api/positions'),
  price: (symbol: string) => fetchJson<QuoteData>(`/api/price/${symbol}`),

  worklist: () => fetchJson<{ symbols: WorklistEntry[]; count: number; max_size: number }>('/api/worklist'),
  worklistAdd: (symbol: string) => post<{ added: boolean }>(`/api/worklist/add/${symbol}`),
  worklistRemove: (symbol: string) => del<{ removed: boolean }>(`/api/worklist/remove/${symbol}`),

  scalperStatus: () => fetchJson<ScalperStatus>('/api/scalper/status'),
  scalperStart: () => post<{ success: boolean }>('/api/scalper/start'),
  scalperStop: () => post<{ success: boolean }>('/api/scalper/stop'),
  scalperEnable: () => post<{ enabled: boolean }>('/api/scalper/enable'),
  scalperDisable: () => post<{ enabled: boolean }>('/api/scalper/disable'),
  scalperTrades: () => fetchJson<{ trades: Trade[]; count: number }>('/api/scalper/trades'),
  scalperWatchlist: () => fetchJson<{ symbols: WatchlistEntry[]; count: number }>('/api/scalper/watchlist'),
  scalperConfig: () => fetchJson<Record<string, unknown>>('/api/scalper/config'),
  scalperUpdateConfig: (updates: Record<string, unknown>) => post<Record<string, unknown>>('/api/scalper/config', updates),
  killSwitch: (activate: boolean) => post<{ kill_switch: boolean }>(`/api/scalper/kill-switch?activate=${activate}`),
  circuitBreakers: () => fetchJson<Record<string, unknown>>('/api/scalper/circuit-breakers'),

  eodToday: () => fetchJson<EODReport>('/api/reports/eod/today'),
  eod: (date: string) => fetchJson<EODReport>(`/api/reports/eod/${date}`),

  watchdog: () => fetchJson<WatchdogStatus>('/api/watchdog/status'),
};
