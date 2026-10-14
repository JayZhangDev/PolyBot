# PolyBot

[![CI](https://github.com/JayZhangDev/PolyBot/actions/workflows/ci.yml/badge.svg)](https://github.com/JayZhangDev/PolyBot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

PolyBot is a research pipeline for [Polymarket](https://polymarket.com) prediction markets. Every hour it snapshots the most-traded markets into SQLite, runs a set of trading signals against that history, and paper-trades them with a fixed bankroll. A Streamlit dashboard shows the book, closed trades, current signals and the equity curve.

The long-term goal is a **calibration tracker**: measuring how well market prices predict outcomes (Brier score, reliability curves) and whether any of the signals beat the market's own probabilities after spread.

<!-- Add a dashboard screenshot: save it as docs/dashboard.png and uncomment the next line. -->
<!-- ![PolyBot dashboard](docs/dashboard.png) -->

## How it works

```mermaid
flowchart LR
    A[Gamma API<br/>top 500 markets by volume] --> C[(SQLite<br/>markets + snapshots)]
    B[Gamma API<br/>markets we hold, by ID] --> C
    C --> D[Signals<br/>longshot fade, momentum]
    C --> W[Watchlist<br/>neg-risk mispricing]
    D --> E[Paper trader<br/>sizing, fills at bid/ask]
    E --> F[(positions +<br/>bankroll events)]
    F --> G[Streamlit dashboard]
    W --> G
```

Each hourly cycle (`python -m polybot.pipeline`):

1. **Snapshot.** Fetch the 500 highest-volume open markets and store price, bid/ask, volume and liquidity.
2. **Refresh held markets.** Any market with an open paper position that wasn't in that set (usually because it closed) is fetched by ID, so resolutions are always seen.
3. **Resolve.** Positions in closed markets settle at $1 or $0 per share; the bankroll is credited.
4. **Trade.** New signals open positions at executable prices (YES at the ask, NO at 1 − bid), 1% of bankroll each, up to 10 open positions and 3 new per strategy per cycle.

## Strategies

| Strategy | Rule | Rationale |
|---|---|---|
| **Longshot fade** | Buy NO when YES ≤ 0.15, volume ≥ $500k, 7+ days to resolution | The favorite–longshot bias: low-probability outcomes tend to be overpriced relative to how often they happen. |
| **Momentum** | Follow a ≥10-point YES move over ~24h, for YES between 0.15 and 0.85 | Tests whether prediction-market prices under-react to news. |
| **Mispricing watchlist** *(not traded)* | Neg-risk events where buying every leg's YES (or NO) at bid/ask costs less than its guaranteed payout | A true arbitrage needs every leg; until whole events are fetched, legs can be missing, so these are leads rather than trades. |

These are hypotheses being tested, not claims of edge. The point of the paper-trading record is to find out.

## Quickstart

```bash
git clone https://github.com/JayZhangDev/PolyBot.git
cd PolyBot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

python -m polybot.pipeline          # one full cycle: snapshot + paper trade
streamlit run polybot/app.py        # dashboard at http://localhost:8501
```

Other entry points:

```bash
python -m polybot.fetch --limit 100   # snapshot only
python -m polybot.signals             # print current signals and watchlist
python -m polybot.portfolio           # print bankroll summary
```

The database lives at `polybot.db` in the project root. Set `POLYBOT_DB=/path/to/file.db` to use a different one. Databases created by v0.1 are migrated in place on first run.

### Running it hourly (macOS)

Save as `~/Library/LaunchAgents/com.jayzhang.polybot.plist`, replacing the paths, then `launchctl load` it:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jayzhang.polybot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/PolyBot/.venv/bin/python</string>
    <string>-m</string><string>polybot.pipeline</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/PolyBot</string>
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key><string>/path/to/PolyBot/launchd.out.log</string>
  <key>StandardErrorPath</key><string>/path/to/PolyBot/launchd.err.log</string>
</dict>
</plist>
```

On Linux, a cron line does the same: `0 * * * * cd /path/to/PolyBot && .venv/bin/python -m polybot.pipeline`.

## Project layout

```
polybot/
  db.py            database location and connections
  fetch.py         Gamma API client (active markets, markets by ID)
  storage.py       schema, migrations, snapshot persistence
  pricing.py       executable entry prices from bid/ask
  signals.py       trading signals and the mispricing watchlist
  portfolio.py     bankroll ledger and position sizing
  paper_trader.py  resolution, settlement and order placement
  pipeline.py      the hourly cycle
  app.py           Streamlit dashboard
tests/             pytest suite (runs against a temporary database)
```

## Testing

```bash
pytest          # 25 tests: pricing, storage migrations, signals, settlement, pipeline
ruff check .
```

CI runs both on every push and pull request, on Python 3.10 and 3.12.

## Roadmap

- [x] **v0.0** Gamma API fetcher and SQLite snapshot storage
- [x] **v0.1** Paper trader, three signals, Streamlit dashboard
- [x] **v0.1.1** Resolution fix for closed markets, bid/ask fills, longshot fade, tests and CI
- [ ] **v0.2** Calibration tracker: record price vs. outcome for every resolved market, Brier score, reliability curve
- [ ] **v0.3** Strategy scorecard: P&L, max drawdown and Brier score per strategy, compared with the market baseline
- [ ] **v0.4** Full-event fetching and a basket executor for neg-risk mispricings

## Limitations

- **Paper only.** No orders are sent anywhere, and fills assume the full stake executes at the top of book.
- **Fees are not modelled.** Taker fees would reduce P&L further.
- **Hourly resolution.** Moves inside the hour are invisible, and momentum compares snapshots about 20 to 24 hours apart.
- **Coverage.** Only the 500 highest-volume open markets are snapshotted each hour, plus any market currently held.

## Disclaimer

This is a research and learning project. Nothing here is financial advice, and the strategies have not been shown to be profitable.

## License

MIT. See [LICENSE](LICENSE).
