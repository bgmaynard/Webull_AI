import { usePolling } from '../hooks/usePolling';
import { api } from '../lib/api';
import type { Trade } from '../lib/api';

export function TradesPanel() {
  const { data } = usePolling(api.scalperTrades, 1000);

  const trades: Trade[] = data?.trades || [];

  return (
    <div className="panel">
      <h3>Open Trades ({trades.length})</h3>
      {trades.length === 0 ? (
        <p className="empty">No active trades</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Entry</th>
              <th>High</th>
              <th>Hold</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t) => (
              <tr key={t.order_id}>
                <td className="symbol">{t.symbol}</td>
                <td>{t.side}</td>
                <td>{t.qty}</td>
                <td>${t.entry_price.toFixed(2)}</td>
                <td>${t.high_since_entry.toFixed(2)}</td>
                <td>{t.hold_seconds.toFixed(0)}s</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
