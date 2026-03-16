import { useCallback, useEffect, useState } from 'react';
import { api } from '../lib/api';
import type { QuoteData } from '../lib/api';

interface QuotePanelProps {
  selectedSymbol?: string | null;
}

export function QuotePanel({ selectedSymbol }: QuotePanelProps) {
  const [symbol, setSymbol] = useState('');
  const [quote, setQuote] = useState<QuoteData | null>(null);
  const [error, setError] = useState('');

  const fetchQuote = useCallback(async (sym?: string) => {
    const target = sym || symbol.trim().toUpperCase();
    if (!target) return;
    setError('');
    try {
      const data = await api.price(target);
      setQuote(data);
      setSymbol(target);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed');
      setQuote(null);
    }
  }, [symbol]);

  // Auto-fetch when a symbol is selected from worklist
  useEffect(() => {
    if (selectedSymbol) {
      fetchQuote(selectedSymbol);
    }
  }, [selectedSymbol]); // eslint-disable-line react-hooks/exhaustive-deps

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
        <button onClick={() => fetchQuote()}>Get Quote</button>
      </div>
      {error && <p className="error">{error}</p>}
      {quote && (
        <div className="quote-grid">
          <div><span className="label">Symbol</span><span className="symbol">{quote.symbol}</span></div>
          <div><span className="label">Price</span><span>${(quote.price ?? 0).toFixed(2)}</span></div>
          {quote.prev_close != null && quote.prev_close > 0 && (
            <div><span className="label">Prev Close</span><span>${quote.prev_close.toFixed(2)}</span></div>
          )}
          <div>
            <span className="label">Change</span>
            <span className={(quote.change_pct ?? 0) >= 0 ? 'profit' : 'loss'}>
              {(quote.change_pct ?? 0) >= 0 ? '+' : ''}{quote.change_pct ?? 0}%
            </span>
          </div>
          <div>
            <span className="label">Bid</span>
            <span>{quote.bid != null && quote.bid > 0 ? `$${quote.bid.toFixed(2)}` : '--'}</span>
          </div>
          <div>
            <span className="label">Ask</span>
            <span>{quote.ask != null && quote.ask > 0 ? `$${quote.ask.toFixed(2)}` : '--'}</span>
          </div>
          <div>
            <span className="label">Spread</span>
            <span>
              {quote.spread != null && quote.spread > 0
                ? `$${quote.spread.toFixed(4)} (${quote.spread_pct ?? 0}%)`
                : 'N/A (premarket)'}
            </span>
          </div>
          <div>
            <span className="label">Volume</span>
            <span>{quote.volume != null && quote.volume > 0 ? quote.volume.toLocaleString() : 'N/A (premarket)'}</span>
          </div>
        </div>
      )}
    </div>
  );
}
