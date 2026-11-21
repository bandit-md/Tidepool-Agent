# 🌊 Tidepool

**Find the small caps worth a second look, and skip the ones that would rug you.**

Tidepool scans eight major blockchains for low-cap tokens and ranks them on what actually protects your money: can you sell, is the contract clean, who holds the supply, and is the volume real?

It won't tell you what goes up. Nobody honestly can. It tells you what's *safe enough to research*, and it does that in minutes instead of an afternoon of tab-hopping.

> Research tool. Not financial advice. Low-cap tokens can and do go to zero.

---

## Why this exists

Small-cap crypto is where the upside lives, and also where most of the scams live. The usual routine looks like this:

1. Find a token on a trending list
2. Check the contract on one site
3. Check liquidity on another
4. Check holders on a third
5. Realise 40 minutes later it's a honeypot

Tidepool does steps 1 to 4 for hundreds of tokens at once, throws out the obvious traps, and hands you a short, ranked list with the reasons spelled out.

## What you get

A clean report (Markdown, CSV and JSON) that looks like this:

| # | Token | Chain | MCap | Liq | Exit cap | Score | Conf | Verdict |
|---|---|---|---|---|---|---|---|---|
| 1 | EXAMPLE | Base | $12.4M | $840K | $8.4K | 84.1 | medium | shortlist |
| 2 | SAMPLE | Solana | $6.1M | $410K | $4.1K | 76.3 | medium | shortlist |
| 3 | DEMO | Arbitrum | $22.0M | $1.1M | $11.0K | 68.9 | medium | watch |

*(Illustrative rows, not real tokens or picks.)*

For the top three you also get the address, safety notes, red flags, estimated slippage on $1k and $5k sells, a sensible position-size ceiling, and the warning signs that should make you walk away.

## The checks

| What we look at | Why it matters |
|---|---|
| 🛡️ **Contract safety** (30%) | Honeypots, hidden owners, mint/freeze powers, sneaky taxes, blacklists. The stuff that steals your money. |
| 💧 **Liquidity** (20%) | Can you actually get out? Exit capacity shows the largest sell that won't wreck the price. |
| 👥 **Holder spread** (15%) | If ten wallets own half the supply, you're their exit liquidity. |
| 📈 **Momentum** (10%) | Healthy trading beats a 400% pump that's about to unwind. |
| 🔍 **Wash-trading flags** | Fake volume gets caught: volume far above market cap or liquidity, or the same few wallets trading over and over. |
| 🔓 **Unlock pressure** (5%) | A big gap between fully diluted and circulating value means more supply is coming. |

**Hard rejects skip the scoring entirely.** A honeypot, an active mint *and* freeze authority, a tax above 10%, or several wash-trading signals mean the token is out, whatever else it scores.

**It tells you how sure it is.** Every token gets a confidence level, and any token we couldn't safety-scan is labelled `unverified` and ranked last. No quiet guessing.

## Chains covered

Ethereum · Solana · BNB Chain · Base · Arbitrum · Tron · Avalanche · Polygon
(plus Optimism and Sui on request)

By default it picks the top 8 live by total value locked, so the list stays current.

## Try it

You need Python 3.9 or newer. That's it: no installs, no API keys, no sign-ups.

```bash
# Scan the top 8 chains with default filters
python3 tidepool.py --top-chains 8 --out ./out

# Just Solana and Base, $2M to $30M market cap, top 15
python3 tidepool.py --chains Solana,Base --min-mc 2e6 --max-mc 3e7 --top 15

# Run the built-in tests (offline)
python3 tidepool.py --selftest
```

Default filters: $1M to $50M market cap, at least $100k liquidity, $50k daily volume, and 14 days of history. Change any of them with flags (`--help` lists them all).

## Built for AI agents too

`skill/SKILL.md` turns Tidepool into a skill for AI assistants. The agent runs the script for the heavy lifting, then layers on the research a script can't do: smart-money wallet flows, team and GitHub activity, audits, and narrative. It's told to cite sources, never invent numbers, and never touch wallets or keys.

## Under the hood

- **One file, standard library only.** Easy to audit, nothing to install, nothing in the supply chain to compromise.
- **Free public data:** DefiLlama for chain rankings, GeckoTerminal for pools and prices, GoPlus for contract security and holders.
- **Hardened against hostile tokens:** names and symbols are attacker-controlled, so reports strip hidden characters and block spreadsheet formula and Markdown injection.
- **Rate-limit aware:** built-in retry and backoff. A full 8-chain scan takes a few minutes on free tiers.
- **Tested:** 19 offline tests covering parsing, safety rules, ranking and report output.

## Honest limits

We'd rather you trust the tool than be impressed by it.

- Slippage and exit capacity are **estimates** from a pool model, not live quotes. Get a real DEX quote before trading.
- Holder concentration can still count exchange hot wallets as holders.
- Sui has no GoPlus coverage yet, so Sui tokens show safety as n/a.
- Smart-money flow and narrative aren't scored by the script, so confidence tops out at "medium" by design.
- Always verify a contract address on a second source (official site plus a block explorer) before you buy.

## What's next

- [ ] Smart-money flow adapter (Birdeye / Dune)
- [ ] Watchlist diffing: what changed since your last scan
- [ ] Telegram and Discord alerts for liquidity drops, mint events and unlocks
- [ ] Cross-chain dedupe, so one project shows up once

## Run exit codes

`0` report written · `2` bad arguments · `3` no data returned (network blocked or rate-limited)

---

**Disclaimer:** Tidepool is a research tool. Nothing it outputs is financial advice or a recommendation to buy or sell. Scores measure risk hygiene, not future price. Never invest more than you can afford to lose.
