import { StatusBar } from './components/StatusBar';
import { PositionsPanel } from './components/PositionsPanel';
import { ScalperControls } from './components/ScalperControls';
import { WatchlistPanel } from './components/WatchlistPanel';
import { WorklistPanel } from './components/WorklistPanel';
import { TradesPanel } from './components/TradesPanel';
import { QuotePanel } from './components/QuotePanel';
import { EODReport } from './components/EODReport';
import './App.css';

function App() {
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
          <PositionsPanel />
        </div>
        <div className="col">
          <WatchlistPanel />
          <WorklistPanel />
        </div>
        <div className="col">
          <QuotePanel />
          <EODReport />
        </div>
      </div>
    </div>
  );
}

export default App;
