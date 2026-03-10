import { useCallback, useState } from 'react';
import { api } from '../lib/api';
import type { EODReport as EODReportType } from '../lib/api';

export function EODReport() {
  const [report, setReport] = useState<EODReportType | null>(null);
  const [loading, setLoading] = useState(false);

  const fetchToday = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.eodToday();
      setReport(data);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }, []);

  return (
    <div className="panel">
      <h3>EOD Report</h3>
      <button onClick={fetchToday} disabled={loading}>
        {loading ? 'Loading...' : 'Load Today'}
      </button>
      {report && (
        <div className="report-grid">
          <div><span className="label">Date</span><span>{report.date}</span></div>
          <div><span className="label">Trades</span><span>{report.total_trades}</span></div>
          <div><span className="label">Winners</span><span className="profit">{report.winners}</span></div>
          <div><span className="label">Losers</span><span className="loss">{report.losers}</span></div>
          <div><span className="label">Win Rate</span><span>{report.win_rate}%</span></div>
          <div>
            <span className="label">Total P&L</span>
            <span className={report.total_pnl >= 0 ? 'profit' : 'loss'}>
              ${report.total_pnl.toFixed(2)}
            </span>
          </div>
          <div><span className="label">Profit Factor</span><span>{report.profit_factor}</span></div>
          <div><span className="label">Avg Hold</span><span>{report.avg_hold_seconds.toFixed(0)}s</span></div>
          <div><span className="label">Blocked</span><span>{report.blocked_trades}</span></div>
          <div><span className="label">Events</span><span>{report.total_events}</span></div>
          {Object.keys(report.exit_reasons).length > 0 && (
            <div className="exit-reasons">
              <span className="label">Exit Reasons</span>
              <div>
                {Object.entries(report.exit_reasons).map(([reason, count]) => (
                  <span key={reason} className="tag">{reason}: {count}</span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
