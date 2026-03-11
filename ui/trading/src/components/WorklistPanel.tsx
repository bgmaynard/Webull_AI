import { useCallback, useMemo, useState } from 'react';
import { usePolling } from '../hooks/usePolling';
import { api } from '../lib/api';
import type { EnrichedWorklistEntry } from '../lib/api';

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

type SortKey = 'symbol' | 'score' | 'change_pct';
type SortDir = 'asc' | 'desc';

function formatAge(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function scoreColor(score: number): string {
  if (score >= 25) return '#5cb85c';
  if (score >= 15) return '#f0ad4e';
  return '#d9534f';
}

function changeColor(pct: number | null): string {
  if (pct == null) return '#888';
  if (pct > 0) return '#5cb85c';
  if (pct < 0) return '#d9534f';
  return '#888';
}

function scoreTooltip(b: EnrichedWorklistEntry['score_breakdown']): string {
  if (!b) return '';
  return `Gap: ${b.gap}, Vol: ${b.volume}, RVOL: ${b.rvol}, News: ${b.news}, Scanner: ${b.scanner}`;
}

/** Parse a raw input string into an array of uppercase symbols. */
function parseSymbols(raw: string): string[] {
  return raw
    .toUpperCase()
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export function WorklistPanel() {
  const { data, refetch } = usePolling(api.worklistEnriched, 2000);
  const [input, setInput] = useState('');
  const [addingStatus, setAddingStatus] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>('score');
  const [sortDir, setSortDir] = useState<SortDir>('desc');

  const handleAdd = useCallback(async () => {
    const symbols = parseSymbols(input);
    if (symbols.length === 0) return;

    if (symbols.length === 1) {
      setAddingStatus('Adding...');
      try {
        await api.worklistAdd(symbols[0]);
      } finally {
        setAddingStatus(null);
      }
    } else {
      setAddingStatus(`Adding ${symbols.length} symbols...`);
      try {
        await api.worklistAddBulk(symbols);
      } finally {
        setAddingStatus(null);
      }
    }

    setInput('');
    await refetch();
  }, [input, refetch]);

  const handleRemove = useCallback(
    async (symbol: string) => {
      await api.worklistRemove(symbol);
      await refetch();
    },
    [refetch],
  );

  const handleSort = useCallback(
    (key: SortKey) => {
      if (sortKey === key) {
        setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
      } else {
        setSortKey(key);
        setSortDir(key === 'symbol' ? 'asc' : 'desc');
      }
    },
    [sortKey],
  );

  const rawSymbols: EnrichedWorklistEntry[] = data?.symbols || [];
  const maxSize = data?.max_size || 15;

  const symbols = useMemo(() => {
    const sorted = [...rawSymbols];
    sorted.sort((a, b) => {
      let cmp = 0;
      if (sortKey === 'symbol') {
        cmp = a.symbol.localeCompare(b.symbol);
      } else {
        cmp = (a[sortKey] ?? 0) - (b[sortKey] ?? 0);
      }
      return sortDir === 'asc' ? cmp : -cmp;
    });
    return sorted;
  }, [rawSymbols, sortKey, sortDir]);

  const sortIndicator = (key: SortKey) => {
    if (sortKey !== key) return '';
    return sortDir === 'asc' ? ' \u25B2' : ' \u25BC';
  };

  const thStyle: React.CSSProperties = { cursor: 'pointer', userSelect: 'none' };

  return (
    <div className="panel">
      <h3>Worklist ({symbols.length}/{maxSize})</h3>

      <div className="add-row">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
          placeholder="Add symbols (e.g. AAPL, TSLA NVDA)..."
        />
        <button onClick={handleAdd} disabled={!!addingStatus}>
          {addingStatus || 'Add'}
        </button>
      </div>

      {symbols.length === 0 ? (
        <p className="empty">Worklist empty</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th style={thStyle} onClick={() => handleSort('symbol')}>
                Symbol{sortIndicator('symbol')}
              </th>
              <th>Price</th>
              <th style={thStyle} onClick={() => handleSort('change_pct')}>
                Chg%{sortIndicator('change_pct')}
              </th>
              <th>State</th>
              <th style={thStyle} onClick={() => handleSort('score')}>
                Score{sortIndicator('score')}
              </th>
              <th>News</th>
              <th>Source</th>
              <th>Age</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {symbols.map((s) => (
              <tr key={s.symbol}>
                <td className="symbol">{s.symbol}</td>
                <td>${s.price?.toFixed(2) ?? '--'}</td>
                <td style={{ color: changeColor(s.change_pct) }}>
                  {s.change_pct != null
                    ? `${s.change_pct >= 0 ? '+' : ''}${s.change_pct.toFixed(2)}%`
                    : '--'}
                </td>
                <td
                  style={{
                    color: STATE_COLORS[s.momentum_state] || '#fff',
                    fontWeight: 600,
                    fontSize: '11px',
                  }}
                >
                  {s.momentum_state || '--'}
                </td>
                <td
                  style={{ color: scoreColor(s.score), fontWeight: 600 }}
                  title={s.score_breakdown ? scoreTooltip(s.score_breakdown) : undefined}
                >
                  {s.score}
                </td>
                <td>
                  {s.news_count > 0 ? (
                    <span
                      style={{
                        color: '#f0ad4e',
                        fontSize: '11px',
                      }}
                      title={`${s.news_count} article${s.news_count !== 1 ? 's' : ''}`}
                    >
                      {s.news_count} ({s.news_score})
                    </span>
                  ) : (
                    <span style={{ color: '#555' }}>--</span>
                  )}
                </td>
                <td>{s.source}</td>
                <td>{formatAge(s.age_seconds)}</td>
                <td>
                  <button className="btn-small" onClick={() => handleRemove(s.symbol)}>
                    X
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
