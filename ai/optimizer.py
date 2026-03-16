"""Genetic Algorithm Parameter Optimizer for HFT Scalper.

Evolves optimal config parameters by replaying historical trades against
actual 1-minute price bars from Polygon. Falls back to analytical replay
when bar data is unavailable.

Usage:
    python -m ai.optimizer --date 2026-03-11
    python -m ai.optimizer --date 2026-03-11 --generations 200 --apply
"""

import asyncio
import copy
import json
import logging
import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytz

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")

# --- Gene Space ---

GENE_BOUNDS = {
    "stop_loss_percent":            (1.0, 5.0),
    "profit_target_percent":        (0.5, 5.0),
    "trailing_stop_percent":        (0.3, 3.0),
    "max_hold_seconds":             (120, 1800),
    "symbol_loss_cooldown_seconds": (0, 1800),
    "risk_percent":                 (0.5, 5.0),
    "max_position_count":           (1, 5),
}

INTEGER_GENES = {"max_hold_seconds", "symbol_loss_cooldown_seconds", "max_position_count"}


# --- Data Structures ---

@dataclass
class SimTrade:
    symbol: str
    entry_price: float
    exit_price: float
    qty: int
    pnl: float
    pnl_pct: float
    hold_seconds: float
    exit_reason: str


@dataclass
class SessionResult:
    trades: list[SimTrade] = field(default_factory=list)
    total_pnl: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    trade_count: int = 0
    sharpe_ratio: float = 0.0


# --- Trade Simulation ---

def simulate_trade_bars(bars: list[dict], entry_price: float, entry_ts: int,
                        params: dict, account_size: float = 750.0) -> SimTrade | None:
    """Simulate a single trade using 1-minute bar data.

    Walks forward from entry_ts through bars, applying exit logic that
    mirrors hft_scalper._monitor_positions().
    """
    stop_pct = params.get("stop_loss_percent", 2.5)
    profit_target = params.get("profit_target_percent", 1.5)
    trail_pct = params.get("trailing_stop_percent", 1.5)
    max_hold = params.get("max_hold_seconds", 600)
    risk_pct = params.get("risk_percent", 2.0)

    # Position sizing
    risk_dollars = account_size * (risk_pct / 100)
    risk_per_share = entry_price * (stop_pct / 100)
    if risk_per_share <= 0 or entry_price <= 0:
        return None
    qty = int(risk_dollars / risk_per_share)
    if qty <= 0:
        return None

    # Find entry bar index
    entry_idx = None
    for i, bar in enumerate(bars):
        if bar["t"] >= entry_ts:
            entry_idx = i
            break
    if entry_idx is None:
        return None

    high_since_entry = entry_price

    for i in range(entry_idx, len(bars)):
        bar = bars[i]
        elapsed = (bar["t"] - entry_ts) / 1000.0  # ms to seconds

        # Use bar high/low for intra-bar stop checks
        bar_high = bar["h"]
        bar_low = bar["l"]
        bar_close = bar["c"]

        # Update high water mark
        if bar_high > high_since_entry:
            high_since_entry = bar_high

        # 1. Hard stop — check if bar low breached stop
        pnl_pct_low = ((bar_low - entry_price) / entry_price) * 100
        if pnl_pct_low <= -stop_pct:
            exit_price = entry_price * (1 - stop_pct / 100)
            pnl = (exit_price - entry_price) * qty
            return SimTrade(
                symbol="", entry_price=entry_price, exit_price=round(exit_price, 4),
                qty=qty, pnl=round(pnl, 2),
                pnl_pct=round(-stop_pct, 2), hold_seconds=round(elapsed, 1),
                exit_reason="HARD STOP",
            )

        # 2+3. Profit target + trailing stop
        high_pnl_pct = ((high_since_entry - entry_price) / entry_price) * 100
        if high_pnl_pct >= profit_target:
            drop_from_high = ((high_since_entry - bar_low) / high_since_entry) * 100
            if drop_from_high >= trail_pct:
                exit_price = high_since_entry * (1 - trail_pct / 100)
                pnl_pct_exit = ((exit_price - entry_price) / entry_price) * 100
                pnl = (exit_price - entry_price) * qty
                return SimTrade(
                    symbol="", entry_price=entry_price, exit_price=round(exit_price, 4),
                    qty=qty, pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct_exit, 2), hold_seconds=round(elapsed, 1),
                    exit_reason="TRAILING STOP",
                )

        # 5. Max hold time
        if elapsed >= max_hold:
            pnl_pct_close = ((bar_close - entry_price) / entry_price) * 100
            if pnl_pct_close <= 0:
                pnl = (bar_close - entry_price) * qty
                return SimTrade(
                    symbol="", entry_price=entry_price, exit_price=bar_close,
                    qty=qty, pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct_close, 2), hold_seconds=round(elapsed, 1),
                    exit_reason="MAX HOLD",
                )
            # Winner past max hold — tighter trailing
            drop_from_high = ((high_since_entry - bar_close) / high_since_entry) * 100 if high_since_entry > 0 else 0
            if drop_from_high >= trail_pct * 0.5:
                pnl = (bar_close - entry_price) * qty
                return SimTrade(
                    symbol="", entry_price=entry_price, exit_price=bar_close,
                    qty=qty, pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct_close, 2), hold_seconds=round(elapsed, 1),
                    exit_reason="MAX HOLD TRAIL",
                )

    # End of bars — exit at last close
    if bars:
        last_close = bars[-1]["c"]
        elapsed = (bars[-1]["t"] - entry_ts) / 1000.0
        pnl_pct_final = ((last_close - entry_price) / entry_price) * 100
        pnl = (last_close - entry_price) * qty
        return SimTrade(
            symbol="", entry_price=entry_price, exit_price=last_close,
            qty=qty, pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct_final, 2), hold_seconds=round(elapsed, 1),
            exit_reason="END OF DATA",
        )

    return None


def simulate_trade_analytical(trade: dict, params: dict, account_size: float = 750.0) -> SimTrade:
    """Analytical trade replay — uses recorded entry/exit with parameter adjustments.

    Heuristic: if the recorded exit was a hard stop and our stop is wider,
    assume the trade would have held longer. If our stop is tighter, assume
    it would have been hit earlier. Approximate but works without bar data.
    """
    entry = trade["entry_price"]
    actual_exit = trade["exit_price"]
    actual_pnl_pct = trade["pnl_pct"]
    actual_hold = trade["hold_seconds"]
    actual_reason = trade.get("exit_reason", "")

    stop_pct = params.get("stop_loss_percent", 2.5)
    profit_target = params.get("profit_target_percent", 1.5)
    trail_pct = params.get("trailing_stop_percent", 1.5)
    max_hold = params.get("max_hold_seconds", 600)
    risk_pct = params.get("risk_percent", 2.0)

    # Position sizing
    risk_dollars = account_size * (risk_pct / 100)
    risk_per_share = entry * (stop_pct / 100)
    if risk_per_share <= 0 or entry <= 0:
        return SimTrade(symbol=trade["symbol"], entry_price=entry, exit_price=entry,
                        qty=0, pnl=0, pnl_pct=0, hold_seconds=0, exit_reason="SKIP")
    qty = int(risk_dollars / risk_per_share)

    # Estimate intra-trade extremes from the actual outcome
    # If the trade hit a hard stop, the low was at least that stop level
    min_pnl_pct = actual_pnl_pct  # worst point during trade
    max_pnl_pct = max(actual_pnl_pct, 0)  # best point (at least entry)

    if "TRAILING" in actual_reason or "TRAIL" in actual_reason:
        # Trade went positive then pulled back
        # Estimate high was ~(actual_pnl_pct + trail implied pullback)
        max_pnl_pct = max(actual_pnl_pct + 1.5, actual_pnl_pct * 1.5)

    if "HARD STOP" in actual_reason:
        # The actual low was the stop level, but price may have gone lower
        min_pnl_pct = actual_pnl_pct

    # Simulate with new params
    # Would our hard stop have been hit?
    if min_pnl_pct <= -stop_pct:
        exit_price = entry * (1 - stop_pct / 100)
        pnl = (exit_price - entry) * qty
        return SimTrade(
            symbol=trade["symbol"], entry_price=entry, exit_price=round(exit_price, 4),
            qty=qty, pnl=round(pnl, 2), pnl_pct=round(-stop_pct, 2),
            hold_seconds=min(actual_hold, actual_hold * (stop_pct / max(abs(actual_pnl_pct), 0.1))),
            exit_reason="HARD STOP",
        )

    # Would trailing stop have triggered?
    if max_pnl_pct >= profit_target:
        # Price hit our profit target, then trail would activate
        trail_exit_pnl = max_pnl_pct - trail_pct
        exit_price = entry * (1 + trail_exit_pnl / 100)
        pnl = (exit_price - entry) * qty
        return SimTrade(
            symbol=trade["symbol"], entry_price=entry, exit_price=round(exit_price, 4),
            qty=qty, pnl=round(pnl, 2), pnl_pct=round(trail_exit_pnl, 2),
            hold_seconds=actual_hold * 0.8,
            exit_reason="TRAILING STOP",
        )

    # Max hold
    if actual_hold >= max_hold:
        if actual_pnl_pct <= 0:
            pnl = (actual_exit - entry) * qty
            return SimTrade(
                symbol=trade["symbol"], entry_price=entry, exit_price=actual_exit,
                qty=qty, pnl=round(pnl, 2), pnl_pct=round(actual_pnl_pct, 2),
                hold_seconds=max_hold,
                exit_reason="MAX HOLD",
            )
        # Winner — tighter trail after max hold
        pnl = (actual_exit - entry) * qty
        return SimTrade(
            symbol=trade["symbol"], entry_price=entry, exit_price=actual_exit,
            qty=qty, pnl=round(pnl, 2), pnl_pct=round(actual_pnl_pct, 2),
            hold_seconds=actual_hold,
            exit_reason="MAX HOLD TRAIL",
        )

    # Default: same outcome, different sizing
    pnl = (actual_exit - entry) * qty
    return SimTrade(
        symbol=trade["symbol"], entry_price=entry, exit_price=actual_exit,
        qty=qty, pnl=round(pnl, 2), pnl_pct=round(actual_pnl_pct, 2),
        hold_seconds=actual_hold,
        exit_reason=actual_reason,
    )


# --- Session Replay ---

def replay_session(trades: list[dict], bars_by_symbol: dict[str, list[dict]],
                   params: dict, account_size: float = 750.0) -> SessionResult:
    """Replay a full session of trades against parameters.

    Handles position limits, symbol cooldowns, and concurrent positions.
    """
    max_positions = int(params.get("max_position_count", 3))
    cooldown_secs = params.get("symbol_loss_cooldown_seconds", 600)
    use_bars = bool(bars_by_symbol)

    sim_trades: list[SimTrade] = []
    active_positions = 0
    symbol_cooldowns: dict[str, float] = {}  # symbol -> cooldown_until (unix seconds)

    # Sort trades by timestamp
    sorted_trades = sorted(trades, key=lambda t: t.get("timestamp", ""))

    for trade in sorted_trades:
        symbol = trade["symbol"]

        # Parse entry time
        try:
            ts_str = trade["timestamp"]
            # Estimate entry time by subtracting hold_seconds from exit timestamp
            exit_dt = datetime.fromisoformat(ts_str)
            entry_dt = exit_dt.timestamp() - trade["hold_seconds"]
        except (ValueError, KeyError):
            entry_dt = 0

        # Check position limit (simplified: assume sequential for analytical)
        # In bar-based mode, we'd track overlaps properly
        if active_positions >= max_positions and not use_bars:
            continue

        # Check symbol cooldown
        cd_until = symbol_cooldowns.get(symbol, 0)
        if entry_dt < cd_until:
            continue

        # Simulate trade
        if use_bars and symbol in bars_by_symbol:
            entry_ts_ms = int(entry_dt * 1000)
            result = simulate_trade_bars(
                bars_by_symbol[symbol], trade["entry_price"],
                entry_ts_ms, params, account_size,
            )
        else:
            result = simulate_trade_analytical(trade, params, account_size)

        if result is None or result.exit_reason == "SKIP":
            continue

        result.symbol = symbol
        sim_trades.append(result)

        # Apply cooldown on loss
        if result.pnl < 0 and cooldown_secs > 0:
            exit_time = entry_dt + result.hold_seconds
            symbol_cooldowns[symbol] = exit_time + cooldown_secs

    # Compute session stats
    return _compute_session_result(sim_trades)


def _compute_session_result(trades: list[SimTrade]) -> SessionResult:
    result = SessionResult()
    result.trades = trades
    result.trade_count = len(trades)

    if not trades:
        return result

    pnls = [t.pnl for t in trades]
    result.total_pnl = round(sum(pnls), 2)

    winners = [t for t in trades if t.pnl > 0]
    losers = [t for t in trades if t.pnl <= 0]
    result.win_rate = round(len(winners) / len(trades) * 100, 1) if trades else 0

    gross_profit = sum(t.pnl for t in winners)
    gross_loss = abs(sum(t.pnl for t in losers))
    if gross_loss > 0:
        result.profit_factor = round(gross_profit / gross_loss, 2)
    elif gross_profit > 0:
        result.profit_factor = 999.0
    else:
        result.profit_factor = 0.0

    # Max drawdown
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    result.max_drawdown = round(max_dd, 2)

    # Sharpe ratio (simplified: mean/std of trade PnLs)
    if len(pnls) > 1:
        mean_pnl = sum(pnls) / len(pnls)
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
        std_pnl = math.sqrt(variance) if variance > 0 else 1.0
        result.sharpe_ratio = round(mean_pnl / std_pnl, 3)
    else:
        result.sharpe_ratio = 0.0

    return result


# --- Fitness Function ---

def fitness(result: SessionResult) -> float:
    """Multi-objective fitness: risk-adjusted return with penalties."""
    # Primary: Sharpe ratio
    sharpe = result.sharpe_ratio * 30.0

    # PnL contribution
    pnl = result.total_pnl * 0.5

    # Win rate bonus above 40%
    winrate = max(0, result.win_rate - 40) * 2.0

    # Drawdown penalty
    drawdown = -result.max_drawdown * 3.0

    # Profit factor bonus
    pf = max(0, result.profit_factor - 1.0) * 20.0

    # Penalize too few trades (overfitting)
    trade_penalty = -max(0, 10 - result.trade_count) * 5.0

    return sharpe + pnl + winrate + drawdown + pf + trade_penalty


# --- GA Operations ---

def random_chromosome() -> dict:
    """Generate a random parameter set within bounds."""
    chrom = {}
    for gene, (lo, hi) in GENE_BOUNDS.items():
        val = random.uniform(lo, hi)
        if gene in INTEGER_GENES:
            val = round(val)
        else:
            val = round(val, 3)
        chrom[gene] = val
    return chrom


def chromosome_from_config(config: dict) -> dict:
    """Extract GA genes from a scalper config."""
    chrom = {}
    for gene, (lo, hi) in GENE_BOUNDS.items():
        val = config.get(gene, (lo + hi) / 2)
        val = max(lo, min(hi, val))
        if gene in INTEGER_GENES:
            val = round(val)
        else:
            val = round(val, 3)
        chrom[gene] = val
    return chrom


def crossover(parent_a: dict, parent_b: dict) -> dict:
    """Uniform crossover — each gene from a random parent."""
    child = {}
    for gene in GENE_BOUNDS:
        child[gene] = parent_a[gene] if random.random() < 0.5 else parent_b[gene]
    return child


def mutate(chrom: dict, mutation_rate: float = 0.2) -> dict:
    """Gaussian mutation per gene."""
    result = dict(chrom)
    for gene, (lo, hi) in GENE_BOUNDS.items():
        if random.random() < mutation_rate:
            spread = (hi - lo) * 0.15
            val = result[gene] + random.gauss(0, spread)
            val = max(lo, min(hi, val))
            if gene in INTEGER_GENES:
                val = round(val)
            else:
                val = round(val, 3)
            result[gene] = val
    return result


def tournament_select(population: list[tuple[dict, float]], k: int = 3) -> dict:
    """Tournament selection — pick best of k random individuals."""
    tournament = random.sample(population, min(k, len(population)))
    return max(tournament, key=lambda x: x[1])[0]


# --- Optimizer ---

_instance = None


def get_optimizer() -> "ParameterOptimizer":
    global _instance
    if _instance is None:
        _instance = ParameterOptimizer()
    return _instance


class ParameterOptimizer:
    def __init__(self):
        self.running: bool = False
        self.progress: dict = {"generation": 0, "total": 0, "best_fitness": 0}
        self.latest_result: dict | None = None
        self._task: asyncio.Task | None = None

    async def run(self, trade_file: str | None = None, date: str | None = None,
                  population_size: int = 50, generations: int = 100,
                  use_bars: bool = True) -> dict:
        """Run the genetic algorithm optimizer."""
        if self.running:
            return {"error": "Optimizer already running"}

        self.running = True
        self.progress = {"generation": 0, "total": generations, "best_fitness": 0}
        start_time = time.time()

        try:
            # Load trades
            if not date:
                date = datetime.now(ET).strftime("%Y-%m-%d")

            trades = self._load_trades(trade_file, date)
            if not trades:
                return {"error": f"No trades found for {date}"}

            symbols = list(set(t["symbol"] for t in trades))
            logger.info("Optimizer: %d trades, %d symbols for %s", len(trades), len(symbols), date)

            # Fetch bar data if requested
            bars_by_symbol: dict[str, list[dict]] = {}
            if use_bars:
                bars_by_symbol = await self._fetch_bars(symbols, date)
                if bars_by_symbol:
                    logger.info("Loaded bars for %d symbols", len(bars_by_symbol))
                else:
                    logger.warning("No bar data available, using analytical replay")

            # Load current config as seed
            current_config = self._load_current_config()
            current_chrom = chromosome_from_config(current_config)

            # Evaluate current config
            current_result = replay_session(trades, bars_by_symbol, current_chrom)
            current_fitness = fitness(current_result)

            # Initialize population
            population: list[tuple[dict, float]] = []

            # Seed with current config
            population.append((current_chrom, current_fitness))

            # Rest are random
            for _ in range(population_size - 1):
                chrom = random_chromosome()
                result = replay_session(trades, bars_by_symbol, chrom)
                fit = fitness(result)
                population.append((chrom, fit))

            convergence = []
            best_ever = max(population, key=lambda x: x[1])

            # GA loop
            for gen in range(generations):
                new_pop: list[tuple[dict, float]] = []

                # Elitism: keep top 2
                population.sort(key=lambda x: x[1], reverse=True)
                new_pop.append(population[0])
                new_pop.append(population[1])

                # Breed rest
                while len(new_pop) < population_size:
                    if random.random() < 0.8:
                        p1 = tournament_select(population)
                        p2 = tournament_select(population)
                        child = crossover(p1, p2)
                    else:
                        child = tournament_select(population)

                    child = mutate(child)
                    result = replay_session(trades, bars_by_symbol, child)
                    fit = fitness(result)
                    new_pop.append((child, fit))

                population = new_pop
                gen_best = max(population, key=lambda x: x[1])

                if gen_best[1] > best_ever[1]:
                    best_ever = gen_best

                self.progress = {
                    "generation": gen + 1,
                    "total": generations,
                    "best_fitness": round(best_ever[1], 2),
                }

                if gen % 10 == 0:
                    convergence.append({
                        "generation": gen,
                        "fitness": round(gen_best[1], 2),
                    })

            # Final evaluation of best
            best_chrom = best_ever[0]
            best_result = replay_session(trades, bars_by_symbol, best_chrom)
            best_fit = fitness(best_result)

            # Build improvement table
            improvement = {}
            for gene in GENE_BOUNDS:
                improvement[gene] = {
                    "current": current_chrom[gene],
                    "recommended": best_chrom[gene],
                    "delta": round(best_chrom[gene] - current_chrom[gene], 3),
                }

            # Build output
            output = {
                "best_params": best_chrom,
                "best_fitness": round(best_fit, 2),
                "current_params": current_chrom,
                "current_fitness": round(current_fitness, 2),
                "improvement": improvement,
                "backtest_comparison": {
                    "current": {
                        "total_pnl": current_result.total_pnl,
                        "win_rate": current_result.win_rate,
                        "profit_factor": current_result.profit_factor,
                        "max_drawdown": current_result.max_drawdown,
                        "trade_count": current_result.trade_count,
                        "sharpe_ratio": current_result.sharpe_ratio,
                    },
                    "optimized": {
                        "total_pnl": best_result.total_pnl,
                        "win_rate": best_result.win_rate,
                        "profit_factor": best_result.profit_factor,
                        "max_drawdown": best_result.max_drawdown,
                        "trade_count": best_result.trade_count,
                        "sharpe_ratio": best_result.sharpe_ratio,
                    },
                },
                "convergence": convergence,
                "warnings": self._generate_warnings(trades, best_result),
                "run_metadata": {
                    "population_size": population_size,
                    "generations": generations,
                    "trade_count": len(trades),
                    "symbols": symbols,
                    "date": date,
                    "elapsed_seconds": round(time.time() - start_time, 1),
                    "replay_mode": "bars" if bars_by_symbol else "analytical",
                },
            }

            self.latest_result = output
            logger.info(
                "Optimizer complete: fitness %.2f -> %.2f, PnL $%.2f -> $%.2f in %.1fs",
                current_fitness, best_fit,
                current_result.total_pnl, best_result.total_pnl,
                time.time() - start_time,
            )
            return output

        finally:
            self.running = False

    def _load_trades(self, trade_file: str | None, date: str) -> list[dict]:
        """Load trades from file or saved history."""
        # Try explicit file
        if trade_file:
            path = Path(trade_file)
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("trades", data if isinstance(data, list) else [])

        # Try reports/{date}_trades.json
        path = Path(f"reports/{date}_trades.json")
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("trades", data if isinstance(data, list) else [])

        # Try getting from running scalper
        try:
            from ai.hft_scalper import get_scalper
            scalper = get_scalper()
            trades = scalper.get_trade_history()
            if trades:
                return trades
        except Exception:
            pass

        return []

    def _load_current_config(self) -> dict:
        """Load current scalper config."""
        try:
            config_path = Path("ai/scalper_config.json")
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    async def _fetch_bars(self, symbols: list[str], date: str) -> dict[str, list[dict]]:
        """Fetch 1-minute bars for all symbols."""
        try:
            from ai.price_cache import get_price_cache
            cache = get_price_cache()
            bars = {}
            for symbol in symbols:
                symbol_bars = await cache.get_bars(symbol, date)
                if symbol_bars:
                    bars[symbol] = symbol_bars
            return bars
        except Exception:
            logger.exception("Failed to fetch bar data")
            return {}

    def _generate_warnings(self, trades: list[dict], result: SessionResult) -> list[str]:
        warnings = []
        dates = set()
        for t in trades:
            ts = t.get("timestamp", "")
            if ts:
                dates.add(ts[:10])
        if len(dates) <= 1:
            warnings.append("Only 1 day of data - optimized params may not generalize")
        if result.trade_count < 20:
            warnings.append(f"Low trade count ({result.trade_count}) - higher overfitting risk")
        if result.profit_factor > 5:
            warnings.append("Very high profit factor - likely overfitted")
        return warnings

    def apply_best(self) -> dict | None:
        """Apply the best parameters to the running scalper."""
        if not self.latest_result:
            return None

        try:
            from ai.hft_scalper import get_scalper
            scalper = get_scalper()
            best = self.latest_result["best_params"]
            return scalper.update_config(best)
        except Exception:
            logger.exception("Failed to apply optimized params")
            return None


# --- CLI ---

async def main():
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="GA Parameter Optimizer for HFT Scalper")
    parser.add_argument("--date", default=datetime.now(ET).strftime("%Y-%m-%d"))
    parser.add_argument("--file", default=None, help="Trade history JSON file")
    parser.add_argument("--population", type=int, default=50)
    parser.add_argument("--generations", type=int, default=100)
    parser.add_argument("--no-bars", action="store_true", help="Skip Polygon bar fetch")
    parser.add_argument("--apply", action="store_true", help="Apply best params to scalper")
    args = parser.parse_args()

    # Load .env
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    optimizer = get_optimizer()
    result = await optimizer.run(
        trade_file=args.file,
        date=args.date,
        population_size=args.population,
        generations=args.generations,
        use_bars=not args.no_bars,
    )

    if "error" in result:
        print(f"ERROR: {result['error']}")
        return

    # Print results
    print("\n" + "=" * 60)
    print("GENETIC ALGORITHM OPTIMIZATION RESULTS")
    print("=" * 60)

    print(f"\nDate: {result['run_metadata']['date']}")
    print(f"Trades: {result['run_metadata']['trade_count']}")
    print(f"Replay mode: {result['run_metadata']['replay_mode']}")
    print(f"Elapsed: {result['run_metadata']['elapsed_seconds']}s")

    print(f"\n--- Backtest Comparison ---")
    curr = result["backtest_comparison"]["current"]
    opt = result["backtest_comparison"]["optimized"]
    print(f"{'Metric':<20} {'Current':>12} {'Optimized':>12} {'Delta':>12}")
    print("-" * 56)
    print(f"{'Total PnL':<20} {'$'+str(curr['total_pnl']):>12} {'$'+str(opt['total_pnl']):>12} {'$'+str(round(opt['total_pnl']-curr['total_pnl'],2)):>12}")
    print(f"{'Win Rate':<20} {str(curr['win_rate'])+'%':>12} {str(opt['win_rate'])+'%':>12}")
    print(f"{'Profit Factor':<20} {str(curr['profit_factor']):>12} {str(opt['profit_factor']):>12}")
    print(f"{'Max Drawdown':<20} {'$'+str(curr['max_drawdown']):>12} {'$'+str(opt['max_drawdown']):>12}")
    print(f"{'Trade Count':<20} {curr['trade_count']:>12} {opt['trade_count']:>12}")
    print(f"{'Sharpe Ratio':<20} {curr['sharpe_ratio']:>12} {opt['sharpe_ratio']:>12}")

    print(f"\n--- Parameter Changes ---")
    print(f"{'Parameter':<35} {'Current':>10} {'Recommended':>12} {'Delta':>10}")
    print("-" * 67)
    for gene, vals in result["improvement"].items():
        delta_str = f"{vals['delta']:+.3f}" if vals['delta'] != 0 else "  --"
        print(f"{gene:<35} {vals['current']:>10} {vals['recommended']:>12} {delta_str:>10}")

    print(f"\nFitness: {result['current_fitness']} -> {result['best_fitness']}")

    if result["warnings"]:
        print(f"\nWarnings:")
        for w in result["warnings"]:
            print(f"  - {w}")

    if args.apply:
        print("\nApplying optimized parameters...")
        applied = optimizer.apply_best()
        if applied:
            print("Config updated successfully.")
        else:
            print("Failed to apply config.")

    # Save results
    output_path = Path(f"reports/{args.date}_optimization.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=True, default=str)
    print(f"\nResults saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
