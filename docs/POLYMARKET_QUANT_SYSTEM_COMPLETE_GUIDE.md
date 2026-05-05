# Polymarket Quant System - Complete Chat Consolidation

This document consolidates everything discussed in this chat into one practical reference for `PolyyKing`: formulas, interpretations, implementation logic, risk controls, and architecture guidance.

---

## 1) Core Thesis

Most retail traders lose because they trade narratives and impulses. Durable profitability in prediction markets comes from running a full system:

1. **Expected Value (EV)** - only take mathematically positive trades.
2. **Mispricing Detection** - identify where crowd probability diverges from fair probability.
3. **Position Sizing (Kelly)** - scale bets by edge while controlling variance.
4. **Bayesian Updating** - update probabilities quickly as evidence changes.
5. **Game-Theoretic Execution (Nash-style thinking)** - optimize maker/taker behavior by regime.

The formulas are necessary but not sufficient. Real performance depends on:
- execution quality,
- fees/spread/slippage control,
- calibration quality of probabilities,
- risk limits and kill switches.

---

## 2) Formula #1 - Expected Value (EV)

### Formula

For binary contracts priced at `price` (0 to 1), $1 payout on win:

- `cost = price`
- `payout_if_win = 1 - price`
- `P(win) = p`
- `P(lose) = 1 - p`

EV per contract:

`EV = p * (1 - price) - (1 - p) * price`

Equivalent generic form:

`EV = (P_win * Profit) - (P_lose * Loss)`

### Decision Rule

- Enter only if `EV > 0`
- Better in production: enter only if `EV_net > threshold`, where net EV subtracts fees and execution costs.

### Example

Contract at `0.12`, your probability `0.20`:

`EV = 0.20 * 0.88 - 0.80 * 0.12 = 0.176 - 0.096 = +0.08`

Positive EV.

If your probability is `0.08`:

`EV = 0.08 * 0.88 - 0.92 * 0.12 = 0.0704 - 0.1104 = -0.04`

Negative EV -> skip.

### Production EV (recommended)

`EV_net = EV - taker_fee - expected_spread_cost - expected_slippage`

Trade only if `EV_net > min_required_edge`.

---

## 3) Formula #2 - Longshot Mispricing Framework

Low-priced contracts often look attractive due to high upside multiples, but can be structurally overpriced.

### Implied Probability

`P_implied = price`

### Mispricing Delta

`delta = P_actual_win_rate - P_implied`

Relative mispricing:

`relative_mispricing = delta / P_implied`

### Excess Return Per Trade

For realized outcome `y` in `{0,1}` and entry price `price`:

`r = (y - price) / price`

- win: `r = (1 - price)/price` (large positive multiple)
- lose: `r = -1` (100% loss on stake)

### Practical Interpretation

- Longshots can produce memorable wins but poor average expectancy.
- Near-certainties can have lower fee drag and better realized expectation in some regimes.
- Never assume this universally; validate by category and period on your own data.

---

## 4) Formula #3 - Kelly Criterion (Position Sizing)

Kelly gives the optimal long-run fraction of bankroll under model assumptions.

### Formula

`f* = (b*p - q) / b`

Where:
- `p = P(win)`
- `q = 1 - p`
- `b = net odds = (1 - price)/price`

### Example

At `price = 0.30`, `p = 0.45`:

- `b = 0.70 / 0.30 = 2.333...`
- `q = 0.55`
- `f* = (2.333*0.45 - 0.55)/2.333 ≈ 0.214` (21.4%)

### Fractional Kelly (recommended)

Use:

`f = f* * kelly_fraction`

Typical `kelly_fraction`: `0.10` to `0.25`.

Apply hard caps:
- max bet % per trade (e.g., 1-5%),
- max market exposure,
- max correlated exposure.

### Important Note on Aggressive Examples

Claims like `p=0.78, b=9 -> very high Kelly fraction` imply huge confidence and often fragile assumptions. In noisy live markets, full Kelly is usually too aggressive.

---

## 5) Formula #4 - Bayesian Updating

Bayes updates your prior probability with new evidence.

### Formula

`P(H|E) = [P(E|H) * P(H)] / P(E)`

Expanded binary form:

`P(H|E) = [P(E|H)*P(H)] / ([P(E|H)*P(H)] + [P(E|~H)*P(~H)])`

### Likelihood Ratio (shortcut intuition)

`LR = P(E|H) / P(E|~H)`

- `LR > 1`: evidence supports hypothesis
- `LR < 1`: evidence weakens hypothesis

### Practical Use

- Maintain a current probability per market.
- Update on each meaningful event/tick/news item.
- Recompute EV and size after each update.
- Log every update for auditability and model debugging.

---

## 6) Formula #5 - Nash-Style Execution Thinking

The exact poker-style bluff formula:

`Bluff% = Bet / (Bet + Pot)`

In prediction-market analogies, the deeper point is strategic balance:

- choose maker vs taker mix based on opponent behavior and microstructure,
- adapt as participant quality changes,
- avoid fixed execution habits.

### Execution Principle for Polymarket

Treat execution as an optimization problem:
- use maker flow where spread capture + fee economics dominate,
- use taker only when urgency and expected alpha decay justify crossing,
- adapt by market category, volatility regime, and depth.

---

## 7) Claims Discussed in Chat: How to Treat Them

Chat examples included claims such as:
- very high PnL bots,
- extraordinary win rates,
- large trade counts with minimal losing streaks.

These can be informative as design inspiration, but should be treated as **unverified marketing claims** unless you have:

- full raw fill logs,
- fee/slippage-adjusted realized PnL,
- max drawdown,
- exposure concentration metrics,
- out-of-sample validation.

Use the math; do not trust headline performance without audit-grade evidence.

---

## 8) DRW/HFT Angle - What Matters in Practice

The three equations (EV, Kelly, Bayes) are the minimum math core. Institutional-quality performance also requires:

1. **Pricing Engine**
   - calibrated probabilities from order flow, volatility, and cross-venue context.
2. **Execution Engine**
   - routing logic, maker/taker policy, queue-aware behavior, fill quality tracking.
3. **Risk Engine**
   - fractional Kelly, hard caps, drawdown constraints, kill switches.
4. **Data Infrastructure**
   - low-latency feeds, normalized storage, replay/backtesting pipeline.
5. **Monitoring and Attribution**
   - edge attribution (signal vs execution vs cost), drift alerts, regime detection.

---

## 9) PolyyKing Implementation Blueprint

### Suggested Modules

1. `pricing_engine`
   - outputs calibrated `p_model` for each market interval.
2. `ev_engine`
   - computes `EV`, `EV_net`, and decision flags.
3. `risk_engine`
   - computes fractional Kelly size + caps.
4. `bayes_engine`
   - applies sequential updates, keeps posterior history.
5. `execution_engine`
   - maker/taker decision, order placement policy, urgency controls.
6. `portfolio_engine`
   - net exposure, correlation-aware limits, PnL rollups.
7. `monitoring_engine`
   - calibration dashboards, drift alarms, fill-quality and fee diagnostics.

### Recommended Decision Pipeline

`raw_data -> features -> p_model -> bayes_update -> EV_net -> sizing -> execution -> post-trade attribution`

### Minimum Trade Gate (recommended)

A trade is allowed only if all are true:

1. `EV_net > threshold`
2. `size > min_notional` and `size <= limits`
3. execution quality estimate acceptable
4. risk state healthy (no kill switch active)

---

## 10) Robust Risk Controls (Non-Negotiable)

1. Fractional Kelly only (`0.10` to `0.25` typical).
2. Per-trade notional cap.
3. Per-market exposure cap.
4. Correlated exposure cap (BTC/ETH/event clusters).
5. Time-window loss limits (hourly/daily stop).
6. Model drift kill switch.
7. Liquidity/spread deterioration kill switch.
8. Exchange/API health fail-safe (pause trading).

---

## 11) Metrics That Actually Matter

Do not optimize only for win rate.

Track:
- expectancy per trade,
- EV prediction error,
- calibration curve / reliability by probability bucket,
- Brier score / log loss,
- realized vs predicted edge,
- slippage and spread capture,
- max drawdown,
- return on risk.

Win rate can be high with poor expectancy and hidden tail risk.

---

## 12) Reference Python Snippets (from chat concepts)

### EV

```python
def calculate_ev(market_price, your_probability):
    cost = market_price
    payout = 1.0 - market_price
    ev = (your_probability * payout) - ((1 - your_probability) * cost)
    roi = ev / cost * 100 if cost > 0 else 0.0
    return {
        "ev": round(ev, 4),
        "roi": round(roi, 2),
        "verdict": "BUY" if ev > 0 else "SKIP"
    }
```

### Kelly (fractional + cap)

```python
def kelly_size(bankroll, price, p_win, fraction=0.25, max_bet_pct=0.05):
    if price <= 0 or price >= 1:
        return {"action": "NO BET", "reason": "Invalid price"}
    b = (1 - price) / price
    q = 1 - p_win
    f_star = (b * p_win - q) / b
    if f_star <= 0:
        return {"action": "NO BET", "reason": "Negative edge"}
    f = min(f_star * fraction, max_bet_pct)
    bet = bankroll * f
    return {"f_star": f_star, "f": f, "bet": bet}
```

### Bayes

```python
def bayes_update(prior, p_e_given_h, p_e_given_not_h):
    numerator = p_e_given_h * prior
    denominator = numerator + p_e_given_not_h * (1 - prior)
    if denominator == 0:
        return prior
    return numerator / denominator
```

---

## 13) Final Practical Rules for PolyyKing

1. No trade without `EV_net > 0` (prefer threshold > 0).
2. No full Kelly in production.
3. Probability model must be calibrated, not just accurate.
4. Execution quality can destroy model edge; monitor it continuously.
5. Headlines and X posts are hypotheses, not evidence.
6. Compound only with auditable, repeatable edge.

---

## 14) Bottom Line

The formulas from this chat are genuinely useful and should be built into `PolyyKing`.  
But profitability comes from the complete system around them: probability quality, execution discipline, risk controls, and constant calibration/monitoring.

Use the math as a guardrail against narrative bias, and use infrastructure as the mechanism that converts edge into realized PnL.

The top 50 wallets on Polymarket aren't people with quick fingers. It's code. Someone parsing Twitter before CNN picks it up. Someone holding orders on both sides of the book 24/7. Someone catching price gaps between Polymarket and Kalshi in milliseconds. And all of that code sits on GitHub for free.
I went through 150 repos. 130 of them were trash - abandoned forks or outright malware that steals private keys. 20 are left that actually work. Bots you clone, paste in your key, and run.
MARKET MAKING
Post quotes on both sides of the book and earn the spread. Competition is brutal.
1. Polymarket/poly-market-maker  285 STARS - github.com/Polymarket/poly-market-maker
Official keeper from Polymarket itself. Two strategies: bands and AMM. Python, MIT, Docker Compose. The reference implementation.
2. warproxxx/poly-maker - github.com/warproxxx/poly-maker
Market-maker configured via Google Sheets. Ships with `poly_merger` for position consolidation. Author is honest in the README: "in today's market this bot is not profitable and will lose money. Use it as a reference implementation."
3. elielieli909/polymarket-marketmaking - github.com/elielieli909/polymarket-marketmaking
Minimal bands market-maker. Compact code, easy to read if you want to understand the mechanics.
4. gamma-trade-lab/polymarket-market-maker - github.com/gamma-trade-lab/polymarket-market-maker
Focus on discipline and testability. Central orchestrator between WS events and `perform_trade()`. For people who care about architecture.
5. lorbke/poly-market-maker - github.com/lorbke/poly-market-maker
Active fork of the official one with patches. Handy when something breaks in the upstream repo.
COPY TRADING
You don't need an edge yourself - you need to find a wallet that has one. The orderbook is public, wallets are on-chain. Nowhere to hide.
6. echandsome/polymarket-betting-bot - github.com/echandsome/Polymarket-betting-bot
TypeScript/Node.js. Copy trading + odds-based strategies. MongoDB, configurable copy ratio, encrypted keys. Solid architecture.
7. polymarket-copy-trading-bot - github.com/topics/polymarket-copy-trading-bot
Dozens of implementations in every language. Warning: lots of garbage and active scam repos here, see section 06.
ARBITRAGE
YES + NO < $1 inside Polymarket, or price gaps with Kalshi. The math is simple, the margin is thin.
8. ent0n29/polybot  509 STARS - github.com/ent0n29/polybot
Multi-service infrastructure on Spring Boot. Kafka, ClickHouse, Grafana, Slack. Ships with a complete-set arbitrage strategy for Polymarket up/down binaries. Paper mode by default.
9. realfishsam/prediction-market-arbitrage-bot - github.com/realfishsam/prediction-market-arbitrage-bot
Educational synthetic arbitrage Polymarket <-> Kalshi in JS. Market orders for simultaneous liquidity grab, dry-run, two modes (yolo and conservative).
10. CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot - github.com/CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot
BTC 1h price markets only. FastAPI backend + React dashboard. Minimal niche bot done right.
11. TopTrenDev/polymarket-kalshi-arbitrage-bot - github.com/TopTrenDev/polymarket-kalshi-arbitrage-bot
Rust. On-chain execution on Polygon, orderbook polling every 2s.
AI / LLM
The model estimates probability from news, picks the trade, sizes the position.
12. Polymarket/agents 3.4k STARS - github.com/Polymarket/agents
Official Polymarket framework. CLI, ChromaDB for news, Gamma client, Pydantic models, MIT. 3.4k STARS - by far the most starred repo in the space. It's a skeleton, not a profitable bot out of the box.
13. MrFadiAi/Polymarket-bot - github.com/MrFadiAi/Polymarket-bot
4 strategies in one bot: arbitrage, smart copy trading, trend, DCA. Dashboard at localhost:3001 with a one-click live/dry-run toggle. Layered loss limits.
14. aulekator/Polymarket-BTC-15-Minute-Trading-Bot - github.com/aulekator/Polymarket-BTC-15-Minute-Trading-Bot
AI bot for Polymarket BTC 15m markets, built on NautilusTrader. Detailed architecture breakdown in the author's Medium post. Great template to understand how these bots are wired internally.
15 AI-Powered MCP Server for Polymarket  477 STARS - github.com/caiovicentino/polymarket-mcp-server
MCP server with 45 tools. Plugs into Claude. A bot you control by chatting.
FRAMEWORKS AND ALL-IN-ONE
A chassis you write your strategy into. Data, execution, backtest, dashboard -already there.
16. braedonsaunders/homerun - github.com/braedonsaunders/homerun
Most complete platform out there for Polymarket + Kalshi. 25+ built-in strategies, backtest, walk-forward, Kelly sizing, copy trading, AI scoring. React dashboard. One-command setup.
17. Drakkar-Software/OctoBot-Prediction-Market - github.com/Drakkar-Software/OctoBot-Prediction-Market
OctoBot fork for prediction markets. Visual UI, not Telegram. Copy trading from the leaderboard out of the box. Fully self-custody - your key never leaves your machine.
18. nautechsystems/nautilus_trader 10k STARS - github.com/nautechsystems/nautilus_trader
Production-grade Rust engine. Official Polymarket adapter, supports EOA / Magic / Gnosis Safe. Same strategies for backtest and live with no rewrite.
19. evan-kolberg/prediction-market-backtesting  740 STARS - github.com/evan-kolberg/prediction-market-backtesting
Nautilus extension for backtesting Polymarket and Kalshi. Pairs with #18.
20. Bonus - from github.com/harish-garg/Awesome-Polymarket-Tools
polymarket-trade-copier - copy trader running on blockchain events instead of API polling. polymarket-apis - unified Python framework (CLOB + Gamma + Data + Web3 + WebSocket + GraphQL) with Pydantic models.
MY RESOURCES
I'm adapting several of these repos for Polymarket. You can track them on my public GitHub:
https://github.com/zostaff
Trade on Polymarket:
https://polymarket.com/?r=zostaff
My Telegram channel:
https://t.me/zostaffsmartarc
What You Should Never Run
In February 2026 the verified GitHub org `dev-protocol` (a legitimate Japanese DeFi team since 2019, verified badge) was hijacked. Attackers pushed 20+ scam bots with lookalike names:
 `polymarket-trading-bot`
 `polymarket-copytrading-bot-sport`
 `polymarket-copy-trading-bot-sports`
 `polymarket-sports-trading-bot`
Polished READMEs, inflated star counts, the bot really does connect to the Polymarket API. But `package.json` ships typosquatted npm packages (`levex-refa`, `lint-builder`, `ts-bign`, `big-nunber`) that on postinstall:
Read your `.env` and steal the wallet private key
 Exfiltrate files to attacker-controlled Vercel endpoints
 Open an SSH backdoor on the victim's machine
The attackers actively delete the warning issues filed by victims.
If you ever ran anything from `dev-protocol` - assume your private keys are compromised. Move funds, rotate keys.

30 Days Using a Polymarket Trading Bot: Real Results, Real Alpha, Real Guide
This is not a hook. This is not just another guide. This is the actual story. 
Bookmark this you will need it
About four months ago, Polymarket bots became the loudest conversation in crypto Twitter. Everyone had a thread. Everyone had a strategy. Everyone was an expert.
We were different  not because we were smarter, but because we were actually burning money trying to make it work.
We were among the first to write seriously about Polymarket bot trading. Not theory.  We were in it  running tests, watching positions, losing on strategies that looked perfect on paper and fell apart in live markets. We wrote about the failures too, which is why people kept reading.
But writing about bots and actually building one that works are two completely different things.
What followed was several months of research, dead ends, version after version of code that almost worked, and a gradual understanding of the one thing that actually matters in automated prediction market trading: not the code, not the API connection, not the interface the strategy and the liquidity logic underneath it.
This article is the honest account of how we got there.
Before You Read Further
If you want to skip straight to the bot  how it works, what it does, real video of it running, user results - everything is at centpro.bot. The site has guides, performance data, and the actual mechanics explained in detail.
If you want the story - keep reading. 
Because the story matters for understanding why the bot works the way it does.
Five Versions Before One That Worked
The first bot we built was an arbitrage detector. It looked for price discrepancies across related Polymarket contracts and was supposed to execute when the spread exceeded transaction costs. It worked in backtesting. In live markets, Polymarket's oracle migration from Augur to Chainlink broke every assumption the logic was built on. We lost time, not just money.
Version two was a news-reactive bot - monitoring feeds and entering positions immediately after significant headlines. The latency problem killed it. By the time our bot read a headline, analyzed it, and submitted an order, the price had already moved. We were providing exit liquidity for faster systems, not capturing edge.
Version three was a bonding strategy bot - entering near-certainty contracts at 90¢–97¢ and collecting the spread to resolution. It worked beautifully until it didn't. April 7, 2026. Iran ceasefire. A contract sitting at 97¢ NO resolution flipped overnight. That version taught us more about risk management than anything before it.
Version four got the strategy closer to right but the execution infrastructure was unstable - API connections dropping, auto-claim failing, positions sitting uncollected. Technically functional. Operationally broken.
Version five is what we started testing in March. It's the one that's been running since. It's the one that's available now.
The Numbers From the Last 30 Days
We're not going to fabricate precision we don't have. What we can tell you is what the data shows.
The strategy running since March has produced approximately 75% return on deployed capital over the most recent 30-day testing period.
The starting capital could have been any amount - the percentage is what validates the strategy, not the absolute dollar figure. Percentage returns are what tell you whether the logic works. Dollar returns tell you how much capital you had.
Here's what the trade distribution looked like across the testing period:
The loss figures matter as much as the win figures. A system that wins 68% of trades but loses too much on the other 32% still goes broke. The ratio here - gains roughly 2x losses - is what makes the math work over time.
We also have feedback from the first users who've been running the bot since it opened for access. The results aren't uniform - they never are, because position sizing, starting capital, and market selection all affect outcomes. But the directional consistency has held.
How This Bot Was Actually Built
Two people built this. One is a quant - someone who spent years thinking about strategy logic, probability modeling, and position sizing in structured financial contexts. The other understands how prediction market infrastructure actually works in practice: the CLOB API, Polygon transaction mechanics, auto-claim logic, order book behavior.
The combination matters. A strategy without correct implementation loses money. Correct implementation without a sound strategy loses money. Both are necessary.
The quant's contribution was the theoretical framework - stress testing the strategy against historical data, identifying the conditions under which it generates edge versus the conditions under which it breaks down, and establishing the position sizing rules that keep the portfolio alive through losing streaks.
The technical implementation took that framework and turned it into something that runs continuously, handles its own error states, claims its own winning positions, and doesn't require human intervention to keep operating.
Most people building bots get one of these two things right. Getting both right is what version five required.
The One Thing That Actually Matters: Portfolio Theory
We've written about this in more depth elsewhere - the MIT portfolio framework, Kelly criterion, GL ratio - but here's the version that's directly relevant to running a bot.
The most common way people lose money with a working strategy is correct strategy, wrong sizing. A bot that bets 40% of its bankroll on a single high-confidence signal will eventually hit a losing streak that it cannot recover from, even if the strategy is genuinely good.
The framework we use has three hard constraints that cannot be overridden by any signal, regardless of confidence:
Single position maximum: 6–8% of total bankroll. No exceptions. A single adverse resolution - one phone call, one tweet, one unexpected announcement - should never materially impair the portfolio.
Category exposure maximum: 20% of total bankroll. All geopolitical positions combined, regardless of how many there are, cannot exceed 20%. They are correlated. They move together when something unexpected happens.
Cash reserve minimum: 20% always deployed. This is not idle capital. This is the mechanism that keeps the bot operational through drawdown periods and allows it to capture opportunities that appear after volatile sessions.
The math behind why these constraints matter:
Kelly fraction for Extremistan markets:
f* = (p × b - (1-p)) / b × 0.15

Where:
p = estimated true probability
b = net payout odds
0.15 = conservative fraction for high-variance categories

Hard cap: min(kelly_output, 0.06 × bankroll)
Ignore these constraints and a working strategy becomes a capital destruction machine during its first significant drawdown. Honor them and the same strategy survives and compounds.
What the Bot Actually Does
The strategy targets specific market conditions where the mathematical edge is clearest. We're not going to publish the full logic here - that would eliminate the edge - but the structural description is accurate:
The bot monitors active markets for conditions where the implied probability diverges meaningfully from the estimated true probability. It enters with limit orders only - never market orders, which create slippage that destroys edge on thin books. It monitors open positions continuously, re-evaluates when new information changes the probability estimate, and exits when the edge has been captured or when the original thesis breaks down.
The entire system is built to run without supervision. Not because we wanted to build something hands-off, but because the markets that generate the most edge often move at hours when nobody is watching.
Can You Build This Yourself?
Yes. We've published the architecture, the code structure, and the strategy framework across multiple articles. The 32 key snippets article has the execution layer. The portfolio theory pieces have the sizing framework. The pipeline articles have the signal generation logic.
Building it yourself takes time and iteration. Expect to run through your own version two and version three before finding what works. Expect to lose money on approaches that fail in live markets but looked correct in theory.
The shortcut is using a system that's already been through those iterations.
What's Available Now
The bot is running. It's available for access now. The site has everything that this article deliberately leaves out: the detailed mechanics, real video of the system operating, user results with actual figures, and the guides for getting started.
centpro.bot
I might've found a cheat code for Polymarket BTC 5m

After 100+ iterations of bot logic the win rate finally locked above 50%
Asymmetric payoffs mean the edge prints regardless

1. LevelDetector connected
- 4 sources: Pivot Points + Order Blocks + iFVG + Walls
- Cluster detection within $15 radius
- Strength score with multi-source confirmation bonus
- Wired into heatmap dashboard for visual confirmation

2. Critical bias bug squashed
- Layer Structure was treating blocked setups as aligned
- DOWN entry above a support was passing as valid SILVER
- Bias now properly reflects support FOR the side direction
- Counter-setups that looked correct are filtered out

3. Choppiness false positives killed
- Old logic flagged $1 noise as maximum market chaos
- chop=1.00 was firing on calm sideways markets
- Killed Structure confidence on every entry
- Now ignores moves under $5 noise threshold
- Levels can finally contribute their full strength

4. Position sizing aligned with Polymarket minimums
- Bronze 2.3% Silver 2.8-3.5% Gold 4.5%
- Above the 5-contract minimum so sizes actually differentiate
- Adaptive sizing instead of every setup landing at the same number

5. Smart Guard filters built from real loss patterns
- Counter-trend block only when momentum continues against us
- Mean reversion entries pass through correctly
- Extreme prices under 38c or over 62c blocked
- Dead market filter when BTC moves under $5 in 90 seconds

Where it stands
- LevelDetector picking up support and resistance live
- Bronze entries getting Silver upgrades thanks to the bias fix
- Target 60-65% win rate should print once fixes settle

Next phase reveals whether the structural rebuild delivers
the expected jump from 50% to 55-65% win rate

What is Cyclops
Cyclops is an automated trading bot for the decentralized prediction market Polymarket. It trades "BTC will go up or down in 5 minutes" contracts, analyzing Bitcoin price flows in real time. The goal is to find situations where the market price of a contract deviates from the real probability of the outcome, and enter with a mathematical edge.
Architecture: Multi-Layer Confluence System
Cyclops is built on four independent confirmation layers. Each layer asks its own question about the market and works separately from the others.
Layer 1 - STRUCTURE answers the question: where are we in the macro context? The layer evaluates the BTC trend, movement pattern from historical memory, and the position of price relative to support and resistance levels.
Layer 2 - FLOW looks at the order flow right now: CVD, aggressor pressure, order book imbalance from multiple exchanges, momentum of the contract itself on Polymarket.
Layer 3 - TRIGGER looks for a specific entry trigger: liquidation magnet, Fair Value Gap, Order Flow Imbalance. 
Layer 4 - GUARD is the final check before entry. It blocks overextension, fake impulse, high choppiness and contract price anomalies.
Based on all four layers, each entry is classified:
GOLD - all 4 layers agree and are strong -> fair prob 0.72
SILVER - STRUCTURE + FLOW, optionally TRIGGER -> fair prob 0.60-0.64
BRONZE - FLOW + TRIGGER, structure is neutral -> fair prob 0.54
SKIP - not enough confirmations -> no entry
python
if s_align and f_align and t_active and s_conf >= 0.50 and f_str >= 0.50:
    return {"tier": "GOLD", "fair_prob": 0.72, "size_pct": 0.020}

if s_align and f_align and s_conf >= 0.30 and f_str >= 0.40:
    return {"tier": "SILVER", "fair_prob": 0.64, "size_pct": 0.015}

if f_align and t_active and f_str >= 0.40:
    return {"tier": "BRONZE", "fair_prob": 0.54, "size_pct": 0.010}
Data Engines
PressureEngine aggregates buy and sell pressure signals and decides on the entry direction - UP, DOWN or SKIP. It is the main source of direction: if Pressure says UP, the bot looks toward UP.
HeatmapEngine analyzes the order book heatmap across five channels: order book pressure, CVD, liquidity wall, Coinbase Premium, trade aggressiveness. Even with a signal from Pressure - without confirmation from Heatmap the entry does not happen:
python
if pressure_side != "SKIP" and hm_result["n_signals"] == 0:
    self._last_filter_reason = "no order book confirmation"
    return None
BTCPatternMemory stores up to 240 segments of historical market structure. Each segment describes the shape of BTC movement: structure, range, levels, degree of choppiness. On a new entry the bot searches for similar past situations through a similarity metric and builds a probabilistic directional forecast:
python
def _score_similarity(self, a, b):
    score = 0.30 if a["structure"] == b["structure"] else 0.0
    score += 0.20 if a["level_state"] == b["level_state"] else 0.0
    score += max(0.0, 0.20 - abs(a["move"] - b["move"]) / 150.0)
    score += max(0.0, 0.15 - abs(a["range"] - b["range"]) / 200.0)
    score += max(0.0, 0.15 - abs(a["choppiness"] - b["choppiness"]))
    return float(np.clip(score, 0.0, 1.0))
Smart Kelly scales the bet size by entry quality. Edge, system confidence, remaining market time, BTC momentum and market regime are all taken into account:
python
quality = (0.35 * edge_n) + (0.30 * conf_n) + (0.15 * time_n) + (0.20 * momentum_n) + regime_bonus
cost = 3.5 + (5.0 - 3.5) * quality
Market Regimes
Before each evaluation Cyclops determines the current BTC market regime: strong directional trend, moderate trend, sideways or uncertainty. The regime affects bet size - in VOLATILE_TREND mode Kelly receives an additional bonus. Regime switching is protected from fluctuations: it does not switch more than once every three iterations.
New Filters
1. Entry Window Expansion
The minimum time to market close has been reduced from 2.0 to 1.0 minutes. This gives the bot an additional window to enter in the final phase of a 5-minute market. The Guard Layer independently checks whether there is enough time for the signal to play out.
2. Smart Counter-trend
The old logic blocked any counter-trend entry. The new one distinguishes two fundamentally different scenarios.
A continuing trend is blocked: DOWN when BTC is above the market start point by $10+ and growing faster than +$3/min, and symmetrically for UP.
Mean reversion is allowed through: BTC moved away from the start point but is already turning back. These entries are historically profitable - the market has already reflected the move in the contract price, and real BTC is starting to return.
3. Extreme Price Filter
Entry is blocked if the contract price is below 38 cents or above 62 cents:
python
if entry_px < 0.35 or entry_px > 0.65:
    return
A too-cheap contract means the market has already priced this outcome as unlikely - catching an edge there is statistically unprofitable. The working zone is the premium zone around 43-49 cents, where the market is most uncertain.
4. Dead Market Filter
If more than 90 seconds have passed since the start of the 5-minute market and BTC has moved less than $5 - the market is declared dead and entry is blocked. A quiet market will not have enough impulse to play out the signal in the remaining time.
Preserved Protections
All previously existing mechanisms remain unchanged: block on overextension toward entry beyond 0.15%, choppy market filter at choppiness above 0.70, fake impulse detection, order book data freshness check, price slippage protection, minimum edge of 1.5% for any trade.
Infrastructure
Cyclops runs in event-driven mode - the main loop does not sleep on a timer but waits for events from WebSocket streams across multiple exchanges simultaneously. If a connection drops, REST fallback activates automatically. A visual order book heatmap runs in parallel for real-time monitoring. Notifications about entries, blocks and balance status are sent to Telegram.
Work Done Over the Last 3 Days
Version 118 - Foundation Fix. Replaced hardcoded fair_prob with a realistic calculation. Added hard edge check with a minimum of 2% to enter. OFA and OB are now counted as one effective signal instead of being duplicated. OFA range was narrowed to stop picking up noise. Window Delta was inverted so overextension now reduces confidence rather than boosting it. Replaced fixed bet sizing with real Kelly.
Version 119 - Connecting the Dead Brain. Activated _trade_decision_context which had been written but never called. Enabled full BTC trend analysis, support and resistance detection, and pattern memory. Carry impulse now based on real price action instead of static values.
Version 120 - Multi-Layer Confluence System. Built the core architecture of four independent layers: STRUCTURE, FLOW, TRIGGER and GUARD. Introduced setup tiers GOLD, SILVER, BRONZE and SKIP. Added adaptive position sizing based on setup quality.
Version 121 - Full Cleanup of Old Logic. Removed all duplicate legacy blockers. Layer System became the sole decision-making brain. Entry price zone expanded.
Version 122 - Rollback. Hard filters were applied based on too small a sample. This was a mistake and was rolled back.
Version 123 - Discovery Mode. Reduced bet sizes for safe exploration. Lowered MIN_EDGE. Goal was to collect enough data for proper calibration.
Version 124 - Smart Guard (current). Smart Counter-trend blocks continuing trends and allows mean reversion through. Extreme price filter cuts off entries at the edges of the price range. Dead market filter skips markets where BTC is not moving. Min time reduced to 1 minute for a wider entry window.
Result
Managed to stabilize win rate after a major architecture overhaul. The bot went from chaotic legacy logic to a structured system with 4 analysis layers and targeted filters based on real trading patterns.
Target WR: 65-70%. Achievable through quality setup classification (GOLD/SILVER/BRONZE), targeted filters based on real data, smart blocks instead of hard thresholds, and further calibration after 50-100 trades.

lIVE : https://t.me/cyclops_signals

the most expensive belief in your life is "I'm too old to start over"

> 35-year-old marketer in Hong Kong walks out of his job
> no CS degree, no quant chops, no rich uncle
> takes Claude, hooks it to MiroFish, runs 10K simulations per trade
> $360,000 profit in his first month

his Polymarket name is literally Marketing101

the easter egg is the entire thesis: he was where you are 12 months ago

> AI is rewriting the economy in real time
> your peers are arguing about RSU vesting schedules
> 30+ isn't late. it's the cleanest entry you'll ever get
> you have pattern recognition a 22-year-old hasn't earned yet

the people still calling this "gambling" in 2027 will be the ones who couldn't read 2026.

you can at least start by copying someone else’s success - repeat every single trade this guy makes using a TG bot: http://kreo.app/@cvxv666