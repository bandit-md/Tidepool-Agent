# Tidepool

Multi-chain low-cap token scout. Finds small-cap tokens across the top 8 blockchains and ranks them on risk hygiene, not price prediction.

Research only. Not financial advice. Low-cap tokens can go to zero.

## Contents
- `tidepool.py` - stdlib-only screener (Python 3.9+, no API keys)
- `skill/SKILL.md` - AI agent skill defining the 9 utilities
- `tests` - run `python3 tidepool.py --selftest` (offline, mocked network)

## Quick start
```
python3 tidepool.py --top-chains 8 --out ./out
python3 tidepool.py --chains Solana,Base --min-mc 2e6 --max-mc 3e7 --top 15
```
Outputs `tidepool_<timestamp>.md/.csv/.json` in `--out`.

## The 9 utilities
| # | Utility | Script | Agent |
|---|---|---|---|
| 1 | Chain resolver | yes | |
| 2 | Low-cap screener | yes | verify address |
| 3 | Liquidity and depth | yes (estimate) | real DEX quote |
| 4 | Contract safety | yes (GoPlus) | explorer, RugCheck |
| 5 | Holders and supply | partial | unlock schedules |
| 6 | Smart money flow | no | Birdeye/Nansen/Arkham |
| 7 | Momentum and wash trading | yes | |
| 8 | Narrative and dev | no | web search, GitHub |
| 9 | Scoring and report | yes | thesis, watchlist |

Score is normalised over checks with live data. Utilities 6 and 8 (20% of weight) are not scored by the script, so confidence never exceeds "medium".

## Known limits
- GeckoTerminal and GoPlus free tiers are rate-limited; a full 8-chain run takes minutes.
- Chain IDs in `CHAIN_MAP` should be verified if a chain returns no data.
- Sui has no GoPlus coverage, so safety is n/a and confidence is low.
- Slippage and exit capacity are x*y=k estimates, not quotes.

## Roadmap
- Birdeye/Dune flow adapter (utility 6)
- Watchlist diffing between runs
- Alert webhooks (Telegram/Discord)
- Cross-chain token dedupe by project
