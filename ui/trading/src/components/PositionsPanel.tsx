import { usePolling } from '../hooks/usePolling';
import { api } from '../lib/api';
import type { Position } from '../lib/api';

export function PositionsPanel() {
  const { data } = usePolling(api.positions, 1000);

  const positions: Position[] = data?.positions || [];

  return (
    <div className="panel">
      <h3>Positions ({positions.length})</h3>
      {positions.length === 0 ? (
        <p className="empty">No open positions</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Qty</th>
              <th>Avg Cost</th>
              <th>Price</th>
              <th>P&L</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.symbol}>
                <td className="symbol">{p.symbol}</td>
                <td>{p.qty}</td>
                <td>${p.avg_cost.toFixed(2)}</td>
                <td>${p.current_price.toFixed(2)}</td>
                <td className={p.unrealized_pnl >= 0 ? 'profit' : 'loss'}>
                  ${p.unrealized_pnl.toFixed(2)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
