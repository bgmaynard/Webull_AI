import { usePolling } from '../hooks/usePolling';
import { api } from '../lib/api';
import type { Status } from '../lib/api';
import { useWebSocket } from '../hooks/useWebSocket';

const PHASE_COLORS: Record<string, string> = {
  OFFHOURS: '#666',
  DISCOVERY: '#f0ad4e',
  LIVE: '#5cb85c',
  EXIT_ONLY: '#d9534f',
  SHADOW: '#5bc0de',
};

const MODE_COLORS: Record<string, string> = {
  SIMULATION: '#f0ad4e',
  PAPER: '#5bc0de',
  LIVE: '#5cb85c',
};

export function StatusBar() {
  const { data } = usePolling<Status>(api.status, 2000);
  const { connected } = useWebSocket();

  if (!data) return <div className="status-bar">Loading...</div>;

  const phaseColor = PHASE_COLORS[data.trading_phase] || '#666';
  const modeColor = MODE_COLORS[data.mode] || '#5cb85c';

  return (
    <div className="status-bar">
      <div className="status-item">
        <span className="label">Server</span>
        <span className="value">{data.version}</span>
      </div>
      <div className="status-item">
        <span className="label">Broker</span>
        <span className="value">{data.broker_provider?.toUpperCase() || '?'}</span>
      </div>
      <div className="status-item">
        <span className="label">Data</span>
        <span className="value">{data.market_data_provider?.toUpperCase() || '?'}</span>
      </div>
      <div className="status-item">
        <span className="label">Time (ET)</span>
        <span className="value">{data.time_et}</span>
      </div>
      <div className="status-item">
        <span className="label">Phase</span>
        <span className="value" style={{ color: phaseColor, fontWeight: 'bold' }}>
          {data.trading_phase}
        </span>
      </div>
      <div className="status-item">
        <span className="label">Mode</span>
        <span className="value" style={{ color: modeColor, fontWeight: 'bold' }}>
          {data.mode}
        </span>
      </div>
      <div className="status-item">
        <span className="label">Auth</span>
        <span className="value" style={{ color: data.authenticated ? '#5cb85c' : '#d9534f' }}>
          {data.authenticated ? 'OK' : 'NO'}
        </span>
      </div>
      <div className="status-item">
        <span className="label">WS</span>
        <span className="value" style={{ color: connected ? '#5cb85c' : '#d9534f' }}>
          {connected ? 'LIVE' : 'OFF'}
        </span>
      </div>
    </div>
  );
}
