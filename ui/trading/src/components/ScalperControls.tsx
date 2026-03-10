import { useCallback, useState } from 'react';
import { usePolling } from '../hooks/usePolling';
import { api } from '../lib/api';
import type { ScalperStatus } from '../lib/api';

export function ScalperControls() {
  const { data, refetch } = usePolling<ScalperStatus>(api.scalperStatus, 1000);
  const [busy, setBusy] = useState(false);

  const action = useCallback(async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      await refetch();
    } catch (e) {
      console.error(e);
    } finally {
      setBusy(false);
    }
  }, [refetch]);

  if (!data) return <div className="panel"><h3>Scalper</h3><p>Loading...</p></div>;

  return (
    <div className="panel">
      <h3>Scalper Controls</h3>
      <div className="controls-grid">
        <div className="control-row">
          <span className="label">Enabled:</span>
          <span className={data.enabled ? 'value-on' : 'value-off'}>
            {data.enabled ? 'YES' : 'NO'}
          </span>
          <button disabled={busy} onClick={() => action(data.enabled ? api.scalperDisable : api.scalperEnable)}>
            {data.enabled ? 'Disable' : 'Enable'}
          </button>
        </div>
        <div className="control-row">
          <span className="label">Running:</span>
          <span className={data.running ? 'value-on' : 'value-off'}>
            {data.running ? 'YES' : 'NO'}
          </span>
          <button disabled={busy || !data.enabled} onClick={() => action(data.running ? api.scalperStop : api.scalperStart)}>
            {data.running ? 'Stop' : 'Start'}
          </button>
        </div>
        <div className="control-row">
          <span className="label">Positions:</span>
          <span className="value">{data.open_positions}</span>
        </div>
        <div className="control-row">
          <span className="label">Session Trades:</span>
          <span className="value">{data.session_trades}</span>
        </div>
        <div className="control-row">
          <span className="label">Kill Switch:</span>
          <span className={data.kill_switch ? 'value-off' : 'value-on'}>
            {data.kill_switch ? 'ACTIVE' : 'OFF'}
          </span>
          <button
            disabled={busy}
            className={data.kill_switch ? '' : 'btn-danger'}
            onClick={() => action(() => api.killSwitch(!data.kill_switch))}
          >
            {data.kill_switch ? 'Deactivate' : 'KILL'}
          </button>
        </div>
      </div>
    </div>
  );
}
