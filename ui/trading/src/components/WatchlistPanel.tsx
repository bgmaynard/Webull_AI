import { usePolling } from '../hooks/usePolling';
import { api } from '../lib/api';
import type { WatchlistEntry } from '../lib/api';

const STATE_COLORS: Record<string, string> = {
  IDLE: '#666',
  CANDIDATE: '#f0ad4e',
  IGNITING: '#ff8c00',
  GATED: '#5bc0de',
  IN_POSITION: '#5cb85c',
  MONITORING: '#9370db',
  EXITING: '#d9534f',
  COOLDOWN: '#999',
};

export function WatchlistPanel() {
  const { data } = usePolling(api.scalperWatchlist, 1000);

  const symbols: WatchlistEntry[] = data?.symbols || [];

  return (
    <div className="panel">
      <h3>Watchlist ({symbols.length})</h3>
      {symbols.length === 0 ? (
        <p className="empty">No active symbols</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>State</th>
              <th>Score</th>
              <th>Time in State</th>
            </tr>
          </thead>
          <tbody>
            {symbols.map((s) => (
              <tr key={s.symbol}>
                <td className="symbol">{s.symbol}</td>
                <td style={{ color: STATE_COLORS[s.state] || '#fff' }}>{s.state}</td>
                <td>{s.score}</td>
                <td>{s.time_in_state.toFixed(0)}s</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
