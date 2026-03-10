import { useCallback, useState } from 'react';
import { usePolling } from '../hooks/usePolling';
import { api } from '../lib/api';
import type { WorklistEntry } from '../lib/api';

export function WorklistPanel() {
  const { data, refetch } = usePolling(api.worklist, 2000);
  const [addSymbol, setAddSymbol] = useState('');

  const handleAdd = useCallback(async () => {
    if (!addSymbol.trim()) return;
    await api.worklistAdd(addSymbol.trim().toUpperCase());
    setAddSymbol('');
    await refetch();
  }, [addSymbol, refetch]);

  const handleRemove = useCallback(async (symbol: string) => {
    await api.worklistRemove(symbol);
    await refetch();
  }, [refetch]);

  const symbols: WorklistEntry[] = data?.symbols || [];
  const maxSize = data?.max_size || 15;

  return (
    <div className="panel">
      <h3>Worklist ({symbols.length}/{maxSize})</h3>
      <div className="add-row">
        <input
          type="text"
          value={addSymbol}
          onChange={(e) => setAddSymbol(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
          placeholder="Add symbol..."
        />
        <button onClick={handleAdd}>Add</button>
      </div>
      {symbols.length === 0 ? (
        <p className="empty">Worklist empty</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Score</th>
              <th>Source</th>
              <th>Age</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {symbols.map((s) => (
              <tr key={s.symbol}>
                <td className="symbol">{s.symbol}</td>
                <td>{s.score}</td>
                <td>{s.source}</td>
                <td>{Math.round(s.age_seconds)}s</td>
                <td>
                  <button className="btn-small" onClick={() => handleRemove(s.symbol)}>X</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
