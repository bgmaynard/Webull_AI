import { useCallback, useState } from 'react';
import { api } from '../lib/api';
import type { QuoteData } from '../lib/api';

export function QuotePanel() {
  const [symbol, setSymbol] = useState('');
  const [quote, setQuote] = useState<QuoteData | null>(null);
  const [error, setError] = useState('');

  const fetchQuote = useCallback(async () => {
    if (!symbol.trim()) return;
    setError('');
    try {
      const data = await api.price(symbol.trim().toUpperCase());
      setQuote(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed');
      setQuote(null);
    }
  }, [symbol]);

  return (
    <div className="panel">
      <h3>Quote Lookup</h3>
      <div className="add-row">
        <input
          type="text"
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && fetchQuote()}
          placeholder="Symbol..."
        />
        <button onClick={fetchQuote}>Get Quote</button>
      </div>
      {error && <p className="error">{error}</p>}
      {quote && (
        <div className="quote-grid">
          <div><span className="label">Symbol</span><span className="symbol">{quote.symbol}</span></div>
          <div><span className="label">Price</span><span>${quote.price.toFixed(2)}</span></div>
          <div><span className="label">Bid</span><span>${quote.bid.toFixed(2)}</span></div>
          <div><span className="label">Ask</span><span>${quote.ask.toFixed(2)}</span></div>
          <div><span className="label">Spread</span><span>${quote.spread.toFixed(4)} ({quote.spread_pct}%)</span></div>
          <div><span className="label">Volume</span><span>{quote.volume.toLocaleString()}</span></div>
          <div>
            <span className="label">Change</span>
            <span className={quote.change_pct >= 0 ? 'profit' : 'loss'}>{quote.change_pct}%</span>
          </div>
        </div>
      )}
    </div>
  );
}
