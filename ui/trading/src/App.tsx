import { useState } from 'react';
import { StatusBar } from './components/StatusBar';
import { PositionsPanel } from './components/PositionsPanel';
import { ScalperControls } from './components/ScalperControls';
import { WatchlistPanel } from './components/WatchlistPanel';
import { WorklistPanel } from './components/WorklistPanel';
import { TradesPanel } from './components/TradesPanel';
import { TradeHistory } from './components/TradeHistory';
import { QuotePanel } from './components/QuotePanel';
import { EODReport } from './components/EODReport';
import './App.css';

function App() {
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);

  return (
    <div className="app">
      <header>
        <h1>Webull Trading Bot</h1>
      </header>
      <StatusBar />
      <div className="dashboard">
        <div className="col">
          <ScalperControls />
          <TradesPanel />
          <TradeHistory />
          <PositionsPanel />
        </div>
        <div className="col">
          <WatchlistPanel />
          <WorklistPanel onSelectSymbol={setSelectedSymbol} />
        </div>
        <div className="col">
          <QuotePanel selectedSymbol={selectedSymbol} />
          <EODReport />
        </div>
      </div>
    </div>
  );
}

export default App;
