#!/usr/bin/env python3
"""
tidepool.py - multi-chain low market cap token screener (stdlib only).

Pipeline (maps to the Tidepool skill):
  1. Resolve top-N chains live by TVL (DefiLlama)
  2. Pull candidate pools per chain (GeckoTerminal, no key needed)
  3. Hard filters: market cap band, liquidity, volume, age, stable/wrapped
  4. Liquidity checks: liq/mcap, estimated slippage, exit capacity
  5. Contract safety + holder distribution (GoPlus, no key needed)
  6. Momentum / wash-trading heuristics
  7. Weighted score, hard-reject overrides, confidence
  8. Reports: Markdown, CSV, JSON

NOT covered here (needs paid/other tools, reported as "n/a", never guessed):
  smart-money flow (10 pts) and narrative/dev signal (10 pts). Score is
  normalised over the weights that had live data, and confidence says so.

Research tool only. Not financial advice. Estimates are labelled as estimates.

Usage:
  python3 tidepool.py --top-chains 8 --min-mc 1e6 --max-mc 5e7 --out ./out
  python3 tidepool.py --chains Solana,Base --top 15
  python3 tidepool.py --selftest
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unittest
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

GT = "https://api.geckoterminal.com/api/v2"
LLAMA_CHAINS = "https://api.llama.fi/v2/chains"
GOPLUS = "https://api.gopluslabs.io/api/v1"

# DefiLlama chain name -> (GeckoTerminal network id, GoPlus chain id or None)
# Verify against the APIs if a chain returns no data; ids occasionally change.
CHAIN_MAP: dict[str, tuple[str, Optional[str]]] = {
    "Ethereum": ("eth", "1"),
    "Solana": ("solana", "solana"),
    "BSC": ("bsc", "56"),
    "Base": ("base", "8453"),
    "Arbitrum": ("arbitrum", "42161"),
    "Tron": ("tron", "tron"),
    "Avalanche": ("avax", "43114"),
    "Polygon": ("polygon_pos", "137"),
    "OP Mainnet": ("optimism", "10"),
    "Sui": ("sui-network", None),  # GoPlus has no Sui token_security: safety = n/a
}
ALIASES = {"bnb": "BSC", "bnb chain": "BSC", "binance": "BSC", "avax": "Avalanche",
           "matic": "Polygon", "optimism": "OP Mainnet", "op": "OP Mainnet",
           "eth": "Ethereum", "sol": "Solana", "arb": "Arbitrum"}
DEFAULT_CHAINS = ["Ethereum", "Solana", "BSC", "Base", "Arbitrum", "Tron", "Avalanche", "Polygon"]

# Base tokens we never treat as "low-cap candidates"
EXCLUDED_SYMBOLS = {
    "USDT", "USDC", "DAI", "USDE", "FDUSD", "TUSD", "USDD", "PYUSD", "USDS", "USD1",
    "WETH", "ETH", "WBTC", "BTC", "CBBTC", "WBNB", "BNB", "SOL", "WSOL", "WAVAX", "AVAX",
    "WMATIC", "POL", "WPOL", "WTRX", "TRX", "SUI", "STETH", "WSTETH", "WEETH", "CBETH", "JITOSOL", "MSOL",
}
# Name fragments that mark bridged / wrapped / staked / LP representations of other assets.
EXCLUDED_NAME_RE = re.compile(r"\b(wrapped|bridged|wormhole|staked|liquid staking|lp token)\b|\(pos\)|\(portal\)",
                              re.IGNORECASE)
DEAD = {"0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000",
        "11111111111111111111111111111111", "1nc1nerator11111111111111111111111111111111"}
# Known AMM pool authorities (lowercased) whose balances are pool reserves, not holders.
POOL_AUTHORITIES = {"5q544fkrfoe6tsebd7s8emxgtjyakttvhaw5q5pge4j1"}  # Raydium AMM v4 authority
GOPLUS_BATCH = 1  # keyless GoPlus answers only the first address of a comma-separated batch

WEIGHTS = {"safety": 30, "liquidity": 20, "distribution": 15, "momentum": 10, "unlock": 5}
UNAVAILABLE_WEIGHTS = {"flow": 10, "narrative": 10}


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def num(x: Any) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def lerp_score(x: Optional[float], bad: float, good: float) -> Optional[float]:
    """Linear 0-100 score: `bad` -> 0, `good` -> 100 (works in either direction)."""
    if x is None:
        return None
    if good == bad:
        return 100.0
    return clamp((x - bad) / (good - bad) * 100.0)


def parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (any fractional precision, 'Z' or offset). None if unparseable."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip().replace("Z", "+00:00")
    m = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", v)
    if m:  # Python <3.11 fromisoformat only accepts 3 or 6 fractional digits
        frac = (m.group(2) or "")[:7].ljust(7, "0") if m.group(2) else ""
        v = m.group(1) + frac + m.group(3)
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def clean_text(value: Any, limit: int = 40) -> str:
    """Neutralise untrusted token names/symbols: strip control and bidi characters, cap length."""
    t = _CTRL.sub("", str(value if value is not None else "")).strip()
    return (t[:limit] + "...") if len(t) > limit else t


def md_cell(value: Any, limit: int = 40) -> str:
    """Untrusted text made safe for a Markdown table cell or line."""
    t = clean_text(value, limit)
    for ch in "\\|`*_[]<>#":
        t = t.replace(ch, "\\" + ch)
    return t


def csv_cell(value: Any) -> Any:
    """Block spreadsheet formula injection for untrusted strings."""
    if isinstance(value, str):
        value = clean_text(value, 500)
        if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + value
    return value


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


class FetchError(Exception):
    pass


class Client:
    """Tiny rate-limited JSON client with retry/backoff. Injectable for tests."""

    MIN_INTERVAL = {"api.geckoterminal.com": 2.2, "api.gopluslabs.io": 1.0, "api.llama.fi": 0.3}

    def __init__(self, timeout: int = 20, retries: int = 3):
        self.timeout, self.retries = timeout, retries
        self._last: dict[str, float] = {}

    def get(self, url: str, params: Optional[dict] = None) -> Any:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        host = urllib.parse.urlparse(url).netloc
        err: Exception | None = None
        for attempt in range(self.retries):
            gap = self.MIN_INTERVAL.get(host, 0.5) - (time.monotonic() - self._last.get(host, 0.0))
            if gap > 0:
                time.sleep(gap)
            self._last[host] = time.monotonic()
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "tidepool/1.0", "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                err = e
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries - 1:
                    retry_after = num(e.headers.get("Retry-After")) if e.headers else None
                    time.sleep(min(retry_after or 2 ** (attempt + 1), 60))
                    continue
                break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                # ValueError covers JSONDecodeError and UnicodeDecodeError on garbage bodies
                err = e
                if attempt < self.retries - 1:
                    time.sleep(2 ** attempt)
        raise FetchError(f"{url} -> {err}")


# ----------------------------------------------------------------------------
# data model
# ----------------------------------------------------------------------------
@dataclass
class Candidate:
    chain: str
    token: str
    symbol: str
    pool: str
    price: Optional[float]
    mc: Optional[float]
    cap_basis: str            # "market_cap" or "fdv"
    fdv: Optional[float]
    liq: float
    vol24: float
    chg24: Optional[float]
    buys: int
    sells: int
    buyers: int
    sellers: int
    age_days: Optional[float]
    name: str = ""
    unique_traders: int = 0
    fetched_at: str = ""
    # filled later
    est_slip_1k_sell: Optional[float] = None
    est_slip_5k_sell: Optional[float] = None
    exit_capacity_usd: Optional[float] = None
    flags: list[str] = field(default_factory=list)
    hard_rejects: list[str] = field(default_factory=list)
    safety: dict = field(default_factory=dict)
    subscores: dict = field(default_factory=dict)
    score: Optional[float] = None
    confidence: str = "low"
    verdict: str = ""


# ----------------------------------------------------------------------------
# 1. chain resolver
# ----------------------------------------------------------------------------
def normalise_chain(name: str) -> Optional[str]:
    n = name.strip()
    for k in CHAIN_MAP:
        if k.lower() == n.lower():
            return k
    return ALIASES.get(n.lower())


def resolve_chains(client: Client, top_n: int) -> list[str]:
    try:
        data = client.get(LLAMA_CHAINS)
        ranked = sorted((d for d in data if d.get("name") in CHAIN_MAP),
                        key=lambda d: d.get("tvl") or 0, reverse=True)
        chains = [d["name"] for d in ranked][:top_n]
        if chains:
            return chains
    except FetchError as e:
        log(f"[warn] DefiLlama chain ranking failed ({e}); using default list")
    return DEFAULT_CHAINS[:top_n]


# ----------------------------------------------------------------------------
# 2. candidate collection (GeckoTerminal)
# ----------------------------------------------------------------------------
def token_address(base_id: str, net: str) -> str:
    """Strip the network prefix from a GeckoTerminal token id.

    Network ids can themselves contain underscores ("polygon_pos_0xabc"), so splitting on the
    first underscore is wrong; strip the known prefix instead.
    """
    prefix = net + "_"
    if base_id.startswith(prefix):
        return base_id[len(prefix):]
    return base_id.rsplit("_", 1)[-1]


def parse_pools(chain: str, payload: dict) -> list[Candidate]:
    out: list[Candidate] = []
    now = datetime.now(timezone.utc)
    fetched = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    net = CHAIN_MAP[chain][0]
    included = {t.get("id"): (t.get("attributes") or {}) for t in (payload or {}).get("included") or []
                if isinstance(t, dict)}
    for p in (payload or {}).get("data") or []:
        try:
            a = p["attributes"]
            base_id = p["relationships"]["base_token"]["data"]["id"]  # e.g. "polygon_pos_0xabc"
            token = token_address(base_id, net)
            meta = included.get(base_id, {})
            symbol = clean_text(meta.get("symbol") or (a.get("name") or "?").split(" / ")[0], 24) or "?"
            mc, fdv = num(a.get("market_cap_usd")), num(a.get("fdv_usd"))
            mc = mc if mc and mc > 0 else None
            fdv = fdv if fdv and fdv > 0 else None
            cap, basis = (mc, "market_cap") if mc else (fdv, "fdv")
            created = parse_ts(a.get("pool_created_at"))
            age = (now - created).total_seconds() / 86400 if created else None
            tx = (a.get("transactions") or {}).get("h24") or {}
            buyers, sellers = int(tx.get("buyers") or 0), int(tx.get("sellers") or 0)
            out.append(Candidate(
                chain=chain, token=token, symbol=symbol, pool=str(a.get("address") or ""),
                price=num(a.get("base_token_price_usd")), mc=cap, cap_basis=basis, fdv=fdv,
                liq=num(a.get("reserve_in_usd")) or 0.0,
                vol24=num((a.get("volume_usd") or {}).get("h24")) or 0.0,
                chg24=num((a.get("price_change_percentage") or {}).get("h24")),
                buys=int(tx.get("buys") or 0), sells=int(tx.get("sells") or 0),
                buyers=buyers, sellers=sellers, age_days=age,
                name=clean_text(meta.get("name") or "", 60),
                unique_traders=int(tx.get("unique_traders") or 0), fetched_at=fetched))
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            continue
    return out


def collect(client: Client, chain: str, pages: int, include_new: bool = True,
            errors: Optional[list] = None) -> list[Candidate]:
    net = CHAIN_MAP[chain][0]
    urls = []
    for pg in range(1, pages + 1):
        urls.append((f"{GT}/networks/{net}/pools", {"page": pg, "sort": "h24_volume_usd_desc", "include": "base_token"}))
        urls.append((f"{GT}/networks/{net}/trending_pools", {"page": pg, "include": "base_token"}))
        if include_new:  # new_pools are hours old; useless when a minimum age filter applies
            urls.append((f"{GT}/networks/{net}/new_pools", {"page": pg, "include": "base_token"}))
    best: dict[str, Candidate] = {}
    # EVM/Tron/Sui ids compare case-insensitively; Solana base58 mints are case-sensitive
    key = (lambda t: t) if net == "solana" else (lambda t: t.lower())
    for url, params in urls:
        try:
            for c in parse_pools(chain, client.get(url, params)):
                cur = best.get(key(c.token))
                if cur is None or c.liq > cur.liq:  # keep deepest pool per token
                    best[key(c.token)] = c
        except FetchError as e:
            log(f"[warn] {chain}: {e}")
            if errors is not None:
                errors.append(f"GeckoTerminal {chain}: {e}")
    return list(best.values())


# ----------------------------------------------------------------------------
# 3. filters
# ----------------------------------------------------------------------------
def apply_filters(cands: list[Candidate], a: argparse.Namespace) -> tuple[list[Candidate], dict[str, int]]:
    kept, reasons = [], {}

    def rej(why: str) -> None:
        reasons[why] = reasons.get(why, 0) + 1

    for c in cands:
        if c.symbol.upper() in EXCLUDED_SYMBOLS or EXCLUDED_NAME_RE.search(c.name or ""):
            rej("stable/wrapped/major")
        elif c.mc is None:
            rej("no market cap or fdv")
        elif not (a.min_mc <= c.mc <= a.max_mc):
            rej("outside market cap band")
        elif c.liq < a.min_liq:
            rej("liquidity too low")
        elif c.vol24 < a.min_vol:
            rej("volume too low")
        elif c.age_days is not None and c.age_days < a.min_age_days:
            rej("too new")
        else:
            kept.append(c)
    return kept, reasons


# ----------------------------------------------------------------------------
# 4. liquidity model (constant-product estimate, labelled as estimate)
# ----------------------------------------------------------------------------
def slippage_estimate(trade_usd: float, liq_usd: float) -> Optional[float]:
    """Price impact % of selling `trade_usd` into a 50/50 x*y=k pool holding `liq_usd` total."""
    if liq_usd <= 0:
        return None
    side = liq_usd / 2.0
    return trade_usd / (side + trade_usd) * 100.0


def exit_capacity(liq_usd: float, max_slip_pct: float = 3.0, depth_cap_pct: float = 1.0) -> float:
    """Largest sell with <= max_slip_pct estimated impact, capped at depth_cap_pct of pool liquidity."""
    side = liq_usd / 2.0
    s = max_slip_pct / 100.0
    by_slip = s * side / (1.0 - s)
    return min(by_slip, liq_usd * depth_cap_pct / 100.0)


# ----------------------------------------------------------------------------
# 5. contract safety + distribution (GoPlus)
# ----------------------------------------------------------------------------
def fetch_goplus(client: Client, chain: str, tokens: list[str], batch_size: int = GOPLUS_BATCH,
                 errors: Optional[list] = None) -> dict[str, dict]:
    gp_id = CHAIN_MAP[chain][1]
    if gp_id is None:
        return {}
    result: dict[str, dict] = {}
    url = (f"{GOPLUS}/solana/token_security" if gp_id == "solana"
           else f"{GOPLUS}/token_security/{gp_id}")
    batch_size = max(1, batch_size)
    for i in range(0, len(tokens), batch_size):
        batch = tokens[i:i + batch_size]
        try:
            payload = client.get(url, {"contract_addresses": ",".join(batch)})
            if not isinstance(payload, dict):
                raise FetchError(f"{url}: unexpected payload")
            if payload.get("code") not in (1, None):
                raise FetchError(f"{url}: GoPlus code {payload.get('code')} {payload.get('message')}")
            for k, v in (payload.get("result") or {}).items():
                if isinstance(v, dict):
                    result[k.lower() if gp_id != "solana" else k] = v
        except FetchError as e:
            log(f"[warn] GoPlus {chain}: {e}")
            if errors is not None:
                errors.append(f"GoPlus {chain}: {e}")
    return result


def _flag(v: Any) -> bool:
    return str(v) == "1"


def _frac(v: Any) -> float:
    n = num(v) or 0.0
    return n


def holders_top10(holders: list[dict], solana: bool) -> Optional[float]:
    """Top-10 share (fraction) excluding contracts/dead/locked (approximates LP/lockers/CEX hot wallets)."""
    if not holders:
        return None
    rows = []
    for h in holders:
        addr = str(h.get("address") or h.get("account") or "").lower()
        if addr in DEAD or addr in POOL_AUTHORITIES or _flag(h.get("is_contract")) or _flag(h.get("is_locked")):
            continue
        rows.append(_frac(h.get("percent")))
    if not rows:
        return None
    if max(rows) > 1.0:  # API returned percentages not fractions
        rows = [r / 100.0 for r in rows]
    return sum(sorted(rows, reverse=True)[:10])


def evaluate_safety(sec: Optional[dict], solana: bool) -> dict:
    """Returns {available, hard_rejects, notes, score, top10, creator_pct, lp_locked_pct}."""
    if not sec:
        return {"available": False, "hard_rejects": [], "notes": ["no GoPlus data"], "score": None,
                "top10": None, "creator_pct": None, "lp_locked_pct": None}
    score, rejects, notes = 100.0, [], []
    creator = None
    lp_locked = None

    if solana:
        mint = _flag((sec.get("mintable") or {}).get("status"))
        freeze = _flag((sec.get("freezable") or {}).get("status"))
        if mint and freeze:
            rejects.append("mint AND freeze authority active")
        elif mint:
            score -= 25; notes.append("mint authority active")
        elif freeze:
            score -= 25; notes.append("freeze authority active")
        if _flag((sec.get("non_transferable"))):
            rejects.append("non-transferable")
        if _flag((sec.get("balance_mutable_authority") or {}).get("status")):
            rejects.append("balances mutable by authority")
        if _flag((sec.get("closable") or {}).get("status")):
            score -= 20; notes.append("closable by authority")
        tf = sec.get("transfer_fee")
        if isinstance(tf, dict) and tf:
            score -= 10; notes.append("transfer fee configured")
        if _flag((sec.get("transfer_fee_upgradable") or {}).get("status")):
            score -= 10; notes.append("transfer fee upgradable")
        if sec.get("transfer_hook"):
            score -= 15; notes.append("transfer hook set")
        if _flag((sec.get("transfer_hook_upgradable") or {}).get("status")):
            score -= 10; notes.append("transfer hook upgradable")
        if _flag((sec.get("metadata_mutable") or {}).get("status")):
            score -= 5; notes.append("metadata mutable")
        top10 = holders_top10(sec.get("holders") or [], True)
        creators = sec.get("creators") or []
        creator = num(creators[0].get("percent")) if creators and isinstance(creators[0], dict) else None
    else:
        if _flag(sec.get("is_honeypot")):
            rejects.append("honeypot")
        if _flag(sec.get("cannot_sell_all")):
            rejects.append("cannot sell all")
        if _flag(sec.get("owner_change_balance")):
            rejects.append("owner can change balances")
        if _flag(sec.get("cannot_buy")):
            rejects.append("cannot buy")
        buy, sell = _frac(sec.get("buy_tax")), _frac(sec.get("sell_tax"))
        if max(buy, sell) > 0.10:
            rejects.append(f"tax >10% (buy {buy:.0%}, sell {sell:.0%})")
        elif max(buy, sell) > 0.05:
            score -= 15; notes.append("tax 5-10%")
        owner = str(sec.get("owner_address") or "").lower()
        if _flag(sec.get("is_mintable")):
            score -= 20; notes.append("mintable")
        if owner and owner not in DEAD:
            score -= 10; notes.append("ownership not renounced")
        for key, pen, label in [("hidden_owner", 25, "hidden owner"),
                                ("can_take_back_ownership", 20, "can reclaim ownership"),
                                ("transfer_pausable", 15, "transfers pausable"),
                                ("slippage_modifiable", 15, "tax modifiable"),
                                ("personal_slippage_modifiable", 15, "per-wallet tax modifiable"),
                                ("is_blacklisted", 10, "blacklist function"),
                                ("is_proxy", 10, "upgradable proxy"),
                                ("trading_cooldown", 5, "trading cooldown"),
                                ("selfdestruct", 25, "self-destruct"),
                                ("external_call", 10, "external call in transfer"),
                                ("honeypot_with_same_creator", 20, "creator deployed honeypots before"),
                                ("is_anti_whale", 3, "max-wallet limits")]:
            if _flag(sec.get(key)):
                score -= pen; notes.append(label)
        if str(sec.get("is_open_source")) == "0":
            score -= 30; notes.append("source not verified")
        lps = sec.get("lp_holders") or []
        if lps:
            lp_locked = 0.0
            for h in lps:
                if _flag(h.get("is_locked")) or str(h.get("address", "")).lower() in DEAD:
                    lp_locked += _frac(h.get("percent"))
            if lp_locked < 0.10:
                score -= 25; notes.append("LP <10% locked/burned")
            elif lp_locked < 0.50:
                score -= 12; notes.append("LP <50% locked/burned")
        top10 = holders_top10(sec.get("holders") or [], False)
        creator = num(sec.get("creator_percent"))
        cex = sec.get("is_in_cex")
        if isinstance(cex, dict) and _flag(cex.get("listed")):
            notes.append("CEX listed: " + ", ".join(clean_text(x, 20) for x in (cex.get("cex_list") or [])[:5]))

    return {"available": True, "hard_rejects": rejects, "notes": notes, "score": clamp(score),
            "top10": top10, "creator_pct": creator, "lp_locked_pct": lp_locked}


# ----------------------------------------------------------------------------
# 6/7. momentum, wash flags, scoring
# ----------------------------------------------------------------------------
def momentum(c: Candidate) -> float:
    s = 70.0
    vm = c.vol24 / c.mc if c.mc else 0
    if 0.05 <= vm <= 1.5:
        s += 15
    elif vm > 3:
        s -= 25
    elif vm < 0.01:
        s -= 20
    if c.chg24 is not None:
        if c.chg24 > 300:
            s -= 40; c.flags.append("pump >300% 24h")
        elif c.chg24 < -50:
            s -= 30; c.flags.append("dump >50% 24h")
    tot = c.buys + c.sells
    if tot:
        ratio = c.buys / tot
        if 0.4 <= ratio <= 0.65:
            s += 10
    return clamp(s)


def wash_flags(c: Candidate) -> list[str]:
    f = []
    if c.mc and c.vol24 / c.mc > 3:
        f.append("volume/mcap >3x")
    traders = c.unique_traders or (c.buyers + c.sellers)
    if traders and (c.buys + c.sells) / traders > 10:
        f.append("txns per unique trader >10")
    if c.liq and c.vol24 / c.liq > 15:
        f.append("volume/liquidity >15x")
    return f


def score_candidate(c: Candidate) -> None:
    c.flags, c.hard_rejects = [], []  # idempotent: safe to re-score
    if c.age_days is None:
        c.flags.append("pool age unknown")
    # liquidity
    c.est_slip_1k_sell = slippage_estimate(1000, c.liq)
    c.est_slip_5k_sell = slippage_estimate(5000, c.liq)
    c.exit_capacity_usd = exit_capacity(c.liq)
    liq_ratio = c.liq / c.mc if c.mc else 0
    liq_s = [lerp_score(liq_ratio, 0.0, 0.15), lerp_score(c.est_slip_1k_sell, 10.0, 1.0)]
    liq_s = [x for x in liq_s if x is not None]
    if c.est_slip_1k_sell is not None and c.est_slip_1k_sell > 10:
        c.hard_rejects.append("est. $1k sell slippage >10%")
    sub = {"liquidity": sum(liq_s) / len(liq_s) if liq_s else 0.0}

    # safety
    sub["safety"] = c.safety.get("score")
    c.hard_rejects += c.safety.get("hard_rejects", [])

    # distribution
    dist_parts = []
    t10 = c.safety.get("top10")
    if t10 is not None:
        dist_parts.append(lerp_score(t10, 0.60, 0.20))
        if t10 > 0.40:
            c.flags.append(f"top10 holders {t10:.0%}")
    cp = c.safety.get("creator_pct")
    if cp is not None:
        dist_parts.append(lerp_score(cp, 0.20, 0.0))
        if cp > 0.10:
            c.flags.append(f"creator holds {cp:.0%}")
    sub["distribution"] = sum(dist_parts) / len(dist_parts) if dist_parts else None

    # fdv / mcap (unlock proxy)
    if c.fdv and c.mc and c.cap_basis == "market_cap":
        ratio = c.fdv / c.mc
        sub["unlock"] = lerp_score(ratio, 5.0, 1.0)
        if ratio > 3:
            c.flags.append(f"FDV/MC {ratio:.1f}x")
    else:
        sub["unlock"] = None

    # momentum + wash
    sub["momentum"] = momentum(c)
    wf = wash_flags(c)
    c.flags += wf
    if len(wf) >= 2:
        c.hard_rejects.append("wash-trading flags: " + "; ".join(wf))

    c.subscores = {k: (round(v, 1) if v is not None else None) for k, v in sub.items()}
    avail = {k: w for k, w in WEIGHTS.items() if sub.get(k) is not None}
    wsum = sum(avail.values())
    c.score = round(sum(sub[k] * w for k, w in avail.items()) / wsum, 1) if wsum else None
    covered = sum(avail.values()) / (sum(WEIGHTS.values()) + sum(UNAVAILABLE_WEIGHTS.values()))
    # never "high": flow + narrative are not covered by this script
    c.confidence = "medium" if (c.safety.get("available") and covered >= 0.7) else "low"
    c.verdict = verdict(c)


def verdict(c: Candidate) -> str:
    if c.hard_rejects:
        return "reject"
    if not c.safety.get("available"):
        return "unverified"
    if c.score is not None and c.score >= 70 and c.confidence == "medium":
        return "shortlist"
    if c.score is not None and c.score >= 50:
        return "watch"
    return "weak"


def rank_key(c: Candidate) -> tuple:
    """Safety-verified, higher-confidence tokens always rank above unverified ones, then by score."""
    return ({"medium": 1, "low": 0}.get(c.confidence, 0), c.score or 0.0)


# ----------------------------------------------------------------------------
# 8. reports
# ----------------------------------------------------------------------------
def money(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    for lim, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= lim:
            return f"${x / lim:.2f}{suf}"
    return f"${x:.0f}"


def write_reports(out_dir: str, ranked: list[Candidate], rejected: list[Candidate],
                  filter_reasons: dict[str, int], meta: dict) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    stamp = meta["generated"].replace(":", "").replace("-", "")[:15]
    paths = {k: os.path.join(out_dir, f"tidepool_{stamp}.{k}") for k in ("md", "csv", "json")}

    with open(paths["json"], "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "ranked": [asdict(c) for c in ranked],
                   "rejected": [asdict(c) for c in rejected], "filter_reasons": filter_reasons}, f, indent=2)

    with open(paths["csv"], "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "chain", "symbol", "name", "token", "pool", "mcap_usd", "cap_basis", "fdv_usd", "liq_usd",
                    "vol24_usd", "chg24_pct", "age_days", "est_slip_1k_sell_pct", "est_slip_5k_sell_pct",
                    "exit_capacity_usd", "score", "confidence", "verdict", "safety", "liquidity", "distribution",
                    "momentum", "unlock", "flags", "safety_notes", "fetched_at"])
        for i, c in enumerate(ranked, 1):
            s = c.subscores
            row = [i, c.chain, c.symbol, c.name, c.token, c.pool, c.mc, c.cap_basis, c.fdv, c.liq, c.vol24, c.chg24,
                   round(c.age_days, 1) if c.age_days is not None else None,
                   round(c.est_slip_1k_sell, 2) if c.est_slip_1k_sell is not None else None,
                   round(c.est_slip_5k_sell, 2) if c.est_slip_5k_sell is not None else None,
                   round(c.exit_capacity_usd) if c.exit_capacity_usd is not None else None,
                   c.score, c.confidence, c.verdict,
                   s.get("safety"), s.get("liquidity"), s.get("distribution"), s.get("momentum"),
                   s.get("unlock"), "; ".join(c.flags), "; ".join(c.safety.get("notes", [])), c.fetched_at]
            w.writerow([csv_cell(x) for x in row])

    L = ["# Tidepool Report", "",
         f"Generated: {meta['generated']} UTC | Chains: {', '.join(meta['chains'])}",
         f"Sources: {', '.join(meta.get('sources', []))}",
         f"Filters: mcap {money(meta['min_mc'])}-{money(meta['max_mc'])}, liq >= {money(meta['min_liq'])}, "
         f"vol >= {money(meta['min_vol'])}, age >= {meta['min_age_days']}d",
         f"Safety scan: {'GoPlus' if not meta['no_safety'] else 'SKIPPED (scores unreliable)'}", "",
         "| # | Token | Chain | MCap | Liq | Exit cap* | Score | Conf | Verdict | Flags |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for i, c in enumerate(ranked, 1):
        L.append(f"| {i} | {md_cell(c.symbol, 20)} | {c.chain} | "
                 f"{money(c.mc)}{' (fdv)' if c.cap_basis == 'fdv' else ''} | "
                 f"{money(c.liq)} | {money(c.exit_capacity_usd)} | {c.score if c.score is not None else 'n/a'} | "
                 f"{c.confidence} | {c.verdict} | {md_cell('; '.join(c.flags[:3]), 120) or '-'} |")
    if not ranked:
        L.append("| - | no token passed all filters and hard checks | | | | | | | | |")
    L += ["", "*Exit cap = estimated max sell at <=3% impact, capped at 1% of pool liquidity (x*y=k estimate).",
          "Ranking puts safety-verified (medium confidence) tokens before unverified ones, then sorts by score.", ""]
    for c in ranked[:3]:
        L += [f"### {md_cell(c.symbol, 20)} ({c.chain})",
              f"- Name: {md_cell(c.name, 60) or 'n/a'}",
              f"- Address: `{clean_text(c.token, 100).replace('`', '')}` (verify on an explorer and the official site)",
              f"- Pool: `{clean_text(c.pool, 100).replace('`', '')}` | data fetched {c.fetched_at}",
              f"- Score {c.score} ({c.confidence} confidence, verdict {c.verdict}); sub-scores: {c.subscores}",
              f"- Est. sell slippage: $1k {fmt_pct(c.est_slip_1k_sell)}, $5k {fmt_pct(c.est_slip_5k_sell)}",
              f"- Safety notes: {md_cell(', '.join(c.safety.get('notes', [])), 300) or 'none found'}",
              f"- Flags: {md_cell(', '.join(c.flags), 300) or 'none'}",
              "- Max position band: <=0.5-1% of portfolio, and never above the exit capacity.",
              "- Invalidation: liquidity drop >30%, top-10 concentration rising, mint/authority event, "
              "new tax or blacklist, unlock.", ""]
    L += ["## Rejected", ""]
    if filter_reasons:
        L.append("Filtered before scoring: " + ", ".join(f"{k}: {v}" for k, v in sorted(filter_reasons.items())))
        L.append("")
    if not filter_reasons and not rejected:
        L.append("None.")
    for c in rejected[:25]:
        L.append(f"- {md_cell(c.symbol, 20)} ({c.chain}): {md_cell('; '.join(c.hard_rejects), 200)}")
    if len(rejected) > 25:
        L.append(f"- ... and {len(rejected) - 25} more (see JSON)")
    L += ["", "## Data gaps"]
    for e in meta.get("source_errors", [])[:20]:
        L.append(f"- Source error: {md_cell(e, 200)}")
    if meta.get("safety_missing"):
        L.append(f"- No GoPlus data for {meta['safety_missing']} token(s): marked unverified, ranked last.")
    L += ["- Smart-money flow and narrative/dev signal NOT assessed (20% of full weight).",
          "- Holder concentration excludes contracts/locked wallets; CEX hot wallets may still be counted.",
          "- Slippage and exit capacity are model estimates, not quotes. Verify with a DEX quote before acting.",
          "- Verify every contract address from a second source (official site + explorer).",
          "- Token names and symbols are untrusted third-party data and have been sanitised.",
          "", "Research only. Not financial advice. Low-cap tokens can go to zero."]
    with open(paths["md"], "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return paths


def fmt_pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.2f}%"


# ----------------------------------------------------------------------------
# orchestration
# ----------------------------------------------------------------------------
def validate_args(a: argparse.Namespace) -> Optional[str]:
    if a.min_mc < 0 or a.max_mc <= 0 or a.min_mc > a.max_mc:
        return "--min-mc must be >= 0 and <= --max-mc"
    if a.min_liq < 0 or a.min_vol < 0 or a.min_age_days < 0:
        return "--min-liq, --min-vol and --min-age-days must be >= 0"
    if a.pages < 1 or a.pages > 10:
        return "--pages must be between 1 and 10"
    if a.top < 1:
        return "--top must be >= 1"
    if a.top_chains < 1:
        return "--top-chains must be >= 1"
    if a.goplus_batch < 1:
        return "--goplus-batch must be >= 1"
    return None


def run(a: argparse.Namespace, client: Client) -> int:
    problem = validate_args(a)
    if problem:
        log(f"[error] {problem}")
        return 2
    errors: list[str] = []
    if a.chains:
        chains = []
        for n in a.chains.split(","):
            if not n.strip():
                continue
            k = normalise_chain(n)
            if k is None:
                log(f"[warn] unknown chain '{n.strip()}' skipped (known: {', '.join(CHAIN_MAP)})")
            elif k not in chains:
                chains.append(k)
    else:
        chains = resolve_chains(client, a.top_chains)
    if not chains:
        log("No valid chains."); return 2
    log(f"Chains: {', '.join(chains)}")

    all_kept: list[Candidate] = []
    reasons: dict[str, int] = {}
    total = 0
    for ch in chains:
        cands = collect(client, ch, a.pages, include_new=a.min_age_days < 2, errors=errors)
        total += len(cands)
        kept, r = apply_filters(cands, a)
        log(f"{ch}: {len(cands)} candidates -> {len(kept)} after filters")
        for k, v in r.items():
            reasons[k] = reasons.get(k, 0) + v
        all_kept += kept

    if total == 0:
        log("[error] no pool data returned from GeckoTerminal for any chain "
            "(network blocked or rate-limited?). No report written.")
        return 3

    if not a.no_safety:
        for ch in chains:
            group = [c for c in all_kept if c.chain == ch]
            if not group:
                continue
            sec = fetch_goplus(client, ch, [c.token for c in group], a.goplus_batch, errors)
            solana = CHAIN_MAP[ch][1] == "solana"
            for c in group:
                key = c.token if solana else c.token.lower()
                c.safety = evaluate_safety(sec.get(key), solana)
    else:
        for c in all_kept:
            c.safety = evaluate_safety(None, False)

    for c in all_kept:
        score_candidate(c)

    ranked = sorted((c for c in all_kept if not c.hard_rejects), key=rank_key, reverse=True)
    rejected = [c for c in all_kept if c.hard_rejects]
    ranked_out = ranked[:a.top]
    if len(ranked) < 5:
        log(f"[note] only {len(ranked)} tokens passed; filters were not loosened. Adjust flags if desired.")

    sources = ["DefiLlama (chain ranking)" if not a.chains else None, "GeckoTerminal (pools)",
               None if a.no_safety else "GoPlus (contract safety, holders)"]
    meta = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "chains": chains,
            "sources": [x for x in sources if x],
            "min_mc": a.min_mc, "max_mc": a.max_mc, "min_liq": a.min_liq, "min_vol": a.min_vol,
            "min_age_days": a.min_age_days, "no_safety": a.no_safety, "source_errors": errors,
            "safety_missing": sum(1 for c in all_kept if not c.safety.get("available")) if not a.no_safety else 0}
    paths = write_reports(a.out, ranked_out, rejected, reasons, meta)
    with open(paths["md"], encoding="utf-8") as f:
        print(f.read())
    log("Wrote: " + ", ".join(paths.values()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Multi-chain low-cap token screener (research only).")
    p.add_argument("--chains", help="comma list e.g. Solana,Base (default: top-N by TVL)")
    p.add_argument("--top-chains", type=int, default=8)
    p.add_argument("--min-mc", type=float, default=1e6)
    p.add_argument("--max-mc", type=float, default=5e7)
    p.add_argument("--min-liq", type=float, default=1e5)
    p.add_argument("--min-vol", type=float, default=5e4)
    p.add_argument("--min-age-days", type=float, default=14)
    p.add_argument("--pages", type=int, default=2, help="GeckoTerminal pages per endpoint (rate-limited)")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--out", default="./out")
    p.add_argument("--no-safety", action="store_true", help="skip GoPlus (scores become unreliable)")
    p.add_argument("--goplus-batch", type=int, default=GOPLUS_BATCH,
                   help="addresses per GoPlus request; keep 1 unless you use a keyed GoPlus plan")
    p.add_argument("--selftest", action="store_true")
    return p


# ----------------------------------------------------------------------------
# self tests (offline, mocked network)
# ----------------------------------------------------------------------------
def _pool(token, name, liq, vol, mc, fdv=None, age_days=60, chg=5.0, buys=300, sells=280, buyers=150, sellers=140,
          net="eth"):
    created = datetime.fromtimestamp(time.time() - age_days * 86400, timezone.utc).isoformat()
    return {"attributes": {"name": name, "address": "pool_" + token, "base_token_price_usd": "0.01",
                           "reserve_in_usd": str(liq), "market_cap_usd": str(mc) if mc else None,
                           "fdv_usd": str(fdv or mc), "pool_created_at": created,
                           "volume_usd": {"h24": str(vol)}, "price_change_percentage": {"h24": str(chg)},
                           "transactions": {"h24": {"buys": buys, "sells": sells, "buyers": buyers, "sellers": sellers}}},
            "relationships": {"base_token": {"data": {"id": f"{net}_{token}"}}}}


class FakeClient:
    def __init__(self, pools, sec):
        self.pools, self.sec = pools, sec

    def get(self, url, params=None):
        if "llama" in url:
            return [{"name": "Ethereum", "tvl": 100}, {"name": "Base", "tvl": 50}, {"name": "Sui", "tvl": 5},
                    {"name": "Unknown", "tvl": 999}]
        if "geckoterminal" in url:
            return {"data": self.pools} if "/pools" in url and "trending" not in url and "new" not in url else {"data": []}
        if "gopluslabs" in url:
            # mimic keyless GoPlus: only the first requested address is answered
            first = (params or {}).get("contract_addresses", "").split(",")[0]
            hit = {k: v for k, v in self.sec.items() if k.lower() == first.lower()}
            return {"code": 1, "message": "OK", "result": hit}
        raise FetchError(url)


class DeadClient:
    def get(self, url, params=None):
        raise FetchError(url + " -> blocked")


class SelfTest(unittest.TestCase):
    def args(self, **kw):
        a = build_parser().parse_args([])
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def test_slippage_and_exit(self):
        self.assertAlmostEqual(slippage_estimate(1000, 200000), 1000 / 101000 * 100, places=6)
        cap = exit_capacity(200000)
        self.assertLessEqual(cap, 2000.0)  # <=1% of liquidity
        self.assertGreater(cap, 0)

    def test_chain_resolution_filters_unknown(self):
        self.assertEqual(resolve_chains(FakeClient([], {}), 2), ["Ethereum", "Base"])
        self.assertEqual(normalise_chain("bnb chain"), "BSC")

    def test_filters(self):
        good = _pool("0xgood", "GOOD / WETH", 300000, 120000, 10_000_000)
        usdc = _pool("0xusdc", "USDC / WETH", 3e8, 1e8, 1e10)
        tiny = _pool("0xtiny", "TINY / WETH", 20000, 120000, 5_000_000)
        young = _pool("0xnew", "NEW / WETH", 300000, 120000, 5_000_000, age_days=2)
        big = _pool("0xbig", "BIG / WETH", 300000, 120000, 5e9)
        nocap = _pool("0xnocap", "NOCAP / WETH", 300000, 120000, None)
        nocap["attributes"]["fdv_usd"] = None
        cands = parse_pools("Ethereum", {"data": [good, usdc, tiny, young, big, nocap]})
        kept, why = apply_filters(cands, self.args())
        self.assertEqual([c.symbol for c in kept], ["GOOD"])
        self.assertEqual(len(why), 5)

    def test_dedupe_keeps_deepest_pool(self):
        shallow = _pool("0xdup", "DUP / WETH", 150000, 100000, 8e6)
        deep = _pool("0xdup", "DUP / USDC", 900000, 100000, 8e6)
        c = FakeClient([shallow, deep], {})
        got = collect(c, "Ethereum", 1)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].liq, 900000)

    def test_honeypot_and_wash_rejected(self):
        a = self.args()
        hp = parse_pools("Ethereum", {"data": [_pool("0xhp", "HP / WETH", 300000, 120000, 1e7)]})[0]
        hp.safety = evaluate_safety({"is_honeypot": "1", "is_open_source": "1"}, False)
        score_candidate(hp)
        self.assertTrue(any("honeypot" in r for r in hp.hard_rejects))
        wash = parse_pools("Ethereum", {"data": [_pool("0xw", "W / WETH", 300000, 120000 * 100, 1e7,
                                                       buys=5000, sells=5000, buyers=50, sellers=50)]})[0]
        wash.safety = evaluate_safety({"is_open_source": "1"}, False)
        score_candidate(wash)
        self.assertTrue(any("wash" in r for r in wash.hard_rejects))

    def test_solana_mint_and_freeze_reject(self):
        s = evaluate_safety({"mintable": {"status": "1"}, "freezable": {"status": "1"}}, True)
        self.assertIn("mint AND freeze authority active", s["hard_rejects"])
        one = evaluate_safety({"mintable": {"status": "1"}, "freezable": {"status": "0"}}, True)
        self.assertEqual(one["hard_rejects"], [])
        self.assertLess(one["score"], 100)

    def test_ranking_prefers_safer(self):
        clean = {"is_open_source": "1", "owner_address": "0x" + "0" * 40, "is_mintable": "0",
                 "buy_tax": "0", "sell_tax": "0",
                 "lp_holders": [{"address": "0xlock", "percent": "0.9", "is_locked": 1}],
                 "holders": [{"address": "0x1", "percent": "0.03", "is_contract": 0}]}
        risky = dict(clean, is_mintable="1", hidden_owner="1", owner_address="0xabc",
                     lp_holders=[{"address": "0xdev", "percent": "1", "is_locked": 0}],
                     holders=[{"address": "0x2", "percent": "0.45", "is_contract": 0}])
        pools = [_pool("0xaaa", "CLEAN / WETH", 400000, 150000, 1e7),
                 _pool("0xbbb", "RISKY / WETH", 400000, 150000, 1e7)]
        sec = {"0xaaa": clean, "0xbbb": risky}
        import tempfile
        a = self.args(chains="Ethereum", out=tempfile.mkdtemp(), pages=1)
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run(a, FakeClient(pools, sec))
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertLess(out.index("CLEAN"), out.index("RISKY"))
        self.assertTrue(any(f.endswith(".csv") for f in os.listdir(a.out)))

    def test_polygon_address_parsing(self):
        c = parse_pools("Polygon", {"data": [_pool("0xabc", "ABC / USDT", 300000, 120000, 1e7, net="polygon_pos")]})
        self.assertEqual(c[0].token, "0xabc")
        self.assertEqual(token_address("sui-network_0x2::sui::SUI", "sui-network"), "0x2::sui::SUI")

    def test_included_metadata_and_bridged_exclusion(self):
        pool = _pool("0xaave", "AAVE / WETH", 300000, 120000, 1e7, net="polygon_pos")
        payload = {"data": [pool], "included": [{"id": "polygon_pos_0xaave", "type": "token",
                                                 "attributes": {"symbol": "AAVE", "name": "Aave (PoS)"}}]}
        c = parse_pools("Polygon", payload)
        self.assertEqual(c[0].name, "Aave (PoS)")
        kept, why = apply_filters(c, self.args())
        self.assertEqual(kept, [])
        self.assertIn("stable/wrapped/major", why)

    def test_zero_market_cap_falls_back_to_fdv(self):
        c = parse_pools("Ethereum", {"data": [_pool("0xz", "Z / WETH", 300000, 120000, 0.0, fdv=8e6)]})[0]
        self.assertEqual((c.mc, c.cap_basis), (8e6, "fdv"))

    def test_timestamp_parsing(self):
        self.assertIsNotNone(parse_ts("2025-11-19T09:03:23Z"))
        self.assertIsNotNone(parse_ts("2025-11-19T09:03:23.12Z"))
        self.assertIsNotNone(parse_ts("2025-11-19T09:03:23.123456789+00:00"))
        self.assertIsNone(parse_ts("not a date"))
        bad = _pool("0xb", "B / WETH", 300000, 120000, 1e7)
        bad["attributes"]["pool_created_at"] = "garbage"
        c = parse_pools("Ethereum", {"data": [bad]})
        self.assertEqual(len(c), 1)  # bad timestamp must not drop the pool
        self.assertIsNone(c[0].age_days)

    def test_goplus_one_address_per_request(self):
        sec = {"0xa": {"is_open_source": "1"}, "0xb": {"is_open_source": "1"}, "0xc": {"is_honeypot": "1"}}
        got = fetch_goplus(FakeClient([], sec), "Ethereum", ["0xA", "0xb", "0xc"])
        self.assertEqual(set(got), {"0xa", "0xb", "0xc"})

    def test_untrusted_text_sanitised(self):
        self.assertEqual(csv_cell("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertNotIn("|", md_cell("A|B").replace("\\|", ""))
        self.assertEqual(clean_text("ev\u202eil\nx"), "evilx")
        self.assertLessEqual(len(clean_text("x" * 500, 40)), 43)

    def test_unverified_ranks_below_verified(self):
        clean = {"is_open_source": "1", "owner_address": "0x" + "0" * 40,
                 "holders": [{"address": "0x1", "percent": "0.03", "is_contract": 0}]}
        pools = [_pool("0xaaa", "VERIFIED / WETH", 400000, 150000, 4e7),
                 _pool("0xbbb", "UNKNOWN / WETH", 900000, 150000, 2e6)]
        import tempfile, io, contextlib
        a = self.args(chains="Ethereum", out=tempfile.mkdtemp(), pages=1)
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(run(a, FakeClient(pools, {"0xaaa": clean})), 0)
        out = buf.getvalue()
        self.assertLess(out.index("VERIFIED"), out.index("UNKNOWN"))
        self.assertIn("unverified", out)

    def test_no_data_returns_error(self):
        import tempfile
        a = self.args(chains="Ethereum", out=tempfile.mkdtemp(), pages=1)
        self.assertEqual(run(a, DeadClient()), 3)
        self.assertEqual(os.listdir(a.out), [])

    def test_invalid_args_rejected(self):
        self.assertEqual(run(self.args(min_mc=5e7, max_mc=1e6), DeadClient()), 2)
        self.assertEqual(run(self.args(pages=0), DeadClient()), 2)

    def test_rescoring_is_idempotent(self):
        c = parse_pools("Ethereum", {"data": [_pool("0xi", "I / WETH", 300000, 120000 * 100, 1e7,
                                                    buys=5000, sells=5000, buyers=50, sellers=50)]})[0]
        c.safety = evaluate_safety({"is_open_source": "1"}, False)
        score_candidate(c)
        first = (list(c.flags), list(c.hard_rejects), c.score)
        score_candidate(c)
        self.assertEqual(first, (c.flags, c.hard_rejects, c.score))

    def test_solana_extra_authorities(self):
        s = evaluate_safety({"mintable": {"status": "0"}, "freezable": {"status": "0"},
                             "transfer_hook": [{"address": "x"}], "transfer_fee": {},
                             "holders": [{"account": "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1", "percent": "0.6"},
                                         {"account": "Abc", "percent": "0.05"}]}, True)
        self.assertIn("transfer hook set", s["notes"])
        self.assertNotIn("transfer fee configured", s["notes"])
        self.assertAlmostEqual(s["top10"], 0.05)  # pool authority excluded

    def test_missing_safety_lowers_confidence(self):
        c = parse_pools("Ethereum", {"data": [_pool("0xq", "Q / WETH", 300000, 120000, 1e7)]})[0]
        c.safety = evaluate_safety(None, False)
        score_candidate(c)
        self.assertEqual(c.confidence, "low")
        self.assertIsNone(c.subscores["safety"])


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # token names may hold characters the console cannot encode
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    a = build_parser().parse_args()
    if a.selftest:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(SelfTest)
        res = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if res.wasSuccessful() else 1
    return run(a, Client())


if __name__ == "__main__":
    sys.exit(main())
