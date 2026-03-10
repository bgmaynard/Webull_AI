import { usePolling } from '../hooks/usePolling';
import { api } from '../lib/api';
import type { CompletedTrade, TradeHistoryResponse } from '../lib/api';

function formatHoldTime(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}m ${secs}s`;
}

export function TradeHistory() {
  const { data } = usePolling<TradeHistoryResponse>(api.scalperHistory, 5000);

  const trades: CompletedTrade[] = data?.trades || [];
  const summary = data?.summary || { total_trades: 0, winners: 0, losers: 0, win_rate: 0, total_pnl: 0 };

  return (
    <div className="panel">
      <h3>Trade History ({summary.total_trades})</h3>

      {summary.total_trades > 0 && (
        <div className="trade-history-summary" style={{ marginBottom: 8, fontSize: '0.9em' }}>
          <span>
            W: <strong>{summary.winners}</strong> | L: <strong>{summary.losers}</strong> | Win Rate: <strong>{summary.win_rate.toFixed(1)}%</strong>
          </span>
          {' | '}
          <span style={{ color: summary.total_pnl >= 0 ? '#4caf50' : '#f44336', fontWeight: 'bold' }}>
            P&L: ${summary.total_pnl.toFixed(2)}
          </span>
        </div>
      )}

      {trades.length === 0 ? (
        <p className="empty">No completed trades</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Qty</th>
              <th>Entry</th>
              <th>Exit</th>
              <th>P&L ($)</th>
              <th>P&L (%)</th>
              <th>Hold</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {[...trades].reverse().map((t, i) => (
              <tr key={`${t.symbol}-${t.timestamp}-${i}`}>
                <td className="symbol">{t.symbol}</td>
                <td>{t.qty}</td>
                <td>${t.entry_price.toFixed(2)}</td>
                <td>${t.exit_price.toFixed(2)}</td>
                <td style={{ color: t.pnl >= 0 ? '#4caf50' : '#f44336' }}>
                  {t.pnl >= 0 ? '+' : ''}${t.pnl.toFixed(2)}
                </td>
                <td style={{ color: t.pnl_pct >= 0 ? '#4caf50' : '#f44336' }}>
                  {t.pnl_pct >= 0 ? '+' : ''}{t.pnl_pct.toFixed(2)}%
                </td>
                <td>{formatHoldTime(t.hold_seconds)}</td>
                <td>{t.exit_reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
